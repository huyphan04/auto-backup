#!/usr/bin/env python3
"""
backup.py

Auto-discovering PostgreSQL backup framework.

Discovers PostgreSQL instances in two ways and backs up both:

1. **Docker**: scans `docker ps` for containers whose image looks like
   Postgres, pulls POSTGRES_USER / POSTGRES_DB from the container's own
   environment, and dumps via `docker exec <container> pg_dump ...` (so
   pg_dump always matches the server version, and the DB is never exposed
   over the host network for backup purposes).

2. **Native (host-installed)**: checks for a running `postgresql` systemd
   service and/or a listening Postgres port (5432 by default, configurable)
   that isn't already claimed by a discovered Docker container. For each
   configured native target, credentials are read from that project's own
   `.env` file (path given in config.json) — never duplicated into the
   backup config itself. Dumps via `pg_dump` run directly on the host.

Both kinds compress to gzip locally, upload to SharePoint, and get the same
per-run local + remote retention cleanup.

Usage:
    python3 backup.py --config config.json --credentials credentials.json
    python3 backup.py --dry-run
    python3 backup.py --validate-config
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from sharepoint_uploader import (
    SharePointUploader,
    SharePointAuthError,
    SharePointUploadError,
    load_sharepoint_config,
)

# ---------------------------------------------------------------------------
# Logging: structured JSON-lines to file + human-readable to stderr
# ---------------------------------------------------------------------------


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        if hasattr(record, "extra_fields"):
            payload.update(record.extra_fields)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(log_path: Optional[str]) -> logging.Logger:
    logger = logging.getLogger("auto_backup")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(stream_handler)

    if log_path:
        log_file = Path(log_path)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(JsonLineFormatter())
        logger.addHandler(file_handler)

    return logger


def log_event(logger: logging.Logger, level: int, msg: str, **fields):
    logger.log(level, msg, extra={"extra_fields": fields})


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@dataclass
class PostgresTarget:
    kind: str            # "docker" or "native"
    project_name: str
    # docker-specific
    container_name: Optional[str] = None
    image: Optional[str] = None
    # native-specific
    host: str = "127.0.0.1"
    port: int = 5432
    env_path: Optional[str] = None
    pg_user: Optional[str] = None
    pg_password: Optional[str] = None
    pg_db: Optional[str] = None


def discover_postgres_containers(
    image_patterns: list[str], exclude: list[str]
) -> list[PostgresTarget]:
    """
    Runs `docker ps` and returns every running container whose image matches
    one of image_patterns (case-insensitive substring match).
    """
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    targets: list[PostgresTarget] = []
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        name, image = line.split("\t", 1)
        if name in exclude:
            continue
        if any(pat.lower() in image.lower() for pat in image_patterns):
            targets.append(
                PostgresTarget(
                    kind="docker",
                    container_name=name,
                    project_name=derive_project_name(name),
                    image=image,
                )
            )
    return targets


def derive_project_name(container_name: str, overrides: Optional[dict] = None) -> str:
    """
    Strips common docker-compose suffixes to get a human project name.
    e.g. "kolbrancherxcom-db-1" -> "kolbrancherxcom"
         "postgresdb"          -> "postgresdb"  (no pattern match, kept as-is)
    """
    if overrides and container_name in overrides:
        return overrides[container_name]

    name = container_name
    for suffix in ("-db-1", "-database-1", "-postgres-1", "-db", "-1"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def get_container_env(container_name: str) -> dict:
    """
    Reads POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB directly from the
    container's own environment. This means we never need operators to
    duplicate DB credentials into a separate backup config file.
    """
    result = subprocess.run(
        ["docker", "exec", container_name, "env"],
        capture_output=True,
        text=True,
        check=True,
    )
    env = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


# ---------------------------------------------------------------------------
# Native (host-installed) Postgres discovery
# ---------------------------------------------------------------------------


def is_native_postgres_present(configured_ports: Optional[set[int]] = None) -> bool:
    """
    Best-effort detection of a host-installed (non-Docker) Postgres:
    checks a running systemd `postgresql*` service, OR that at least one of
    the ports actually configured for native targets is listening. This
    deliberately does NOT treat "any port listening" as a signal — plenty
    of non-Postgres services listen on TCP ports, and we only care whether
    the ports our config.json actually points at are alive.
    """
    if _systemd_postgres_active():
        return True
    if configured_ports:
        return bool(configured_ports & _listening_postgres_ports())
    return False


def _systemd_postgres_active() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=running",
             "--no-legend", "--plain"],
            capture_output=True, text=True, timeout=10,
        )
        return bool(re.search(r"postgresql\S*\.service", result.stdout, re.IGNORECASE))
    except Exception:
        return False


def _listening_postgres_ports() -> set[int]:
    """
    Parses `ss -tln` for listening TCP ports. Doesn't require root to see
    that a port is listening (only to see which process owns it), so this
    works even if the backup user isn't root. Matches the local address:port
    anywhere in the line rather than assuming a fixed column, since ss's
    column widths vary.
    """
    try:
        result = subprocess.run(
            ["ss", "-tln"], capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return set()

    ports = set()
    for line in result.stdout.splitlines():
        for m in re.finditer(r"(?:\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|\[::\]|\*|::):(\d+)\b", line):
            ports.add(int(m.group(1)))
    return ports


def load_native_env_file(env_path: str) -> dict:
    """
    Minimal .env parser: KEY=VALUE per line, ignores comments/blank lines,
    strips surrounding quotes. Does not do shell-style variable expansion
    like ${VAR} — if a project's .env relies on that (e.g. a composed
    DB_DSN string built from POSTGRES_USER/POSTGRES_PASS/... via shell
    expansion), point user_key/password_key/db_key at the underlying leaf
    variables instead of the composed DSN.
    """
    env = {}
    path = Path(env_path)
    if not path.exists():
        raise FileNotFoundError(f".env file not found: {env_path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        env[k] = v
    return env


def build_native_targets(native_configs: list[dict], logger: logging.Logger) -> list[PostgresTarget]:
    """
    native_configs: list of entries from config.json["native_postgres"], e.g.
      [{"project_name": "my-project", "env_path": "/opt/my-project/.env",
        "host": "localhost", "port": 5432,
        "user_key": "POSTGRES_USER", "password_key": "POSTGRES_PASSWORD",
        "db_key": "POSTGRES_DB"}]

    IMPORTANT: user_key / password_key / db_key must be the *variable
    names* as they appear in that project's .env file (e.g. "POSTGRES_USER"),
    never the actual username/password/db value itself. This function
    rejects entries where a *_key field doesn't look like a variable name
    (env var names are UPPER_SNAKE_CASE by convention) — that's the
    strongest signal available that someone pasted a literal secret into
    config.json by mistake.

    Only entries whose .env can be read successfully, and whose keys are
    actually present in that .env, are returned; failures are logged and
    skipped rather than aborting the whole run.
    """
    key_name_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    targets = []
    for entry in native_configs:
        project_name = entry["project_name"]
        env_path = entry["env_path"]
        user_key = entry.get("user_key", "POSTGRES_USER")
        pass_key = entry.get("password_key", "POSTGRES_PASSWORD")
        db_key = entry.get("db_key", "POSTGRES_DB")

        bad_keys = [
            (label, val) for label, val in
            (("user_key", user_key), ("password_key", pass_key), ("db_key", db_key))
            if not key_name_re.match(val)
        ]
        if bad_keys:
            log_event(
                logger, logging.ERROR,
                "native_postgres entry rejected: *_key fields must be .env VARIABLE NAMES, "
                "not actual values — config.json should never contain real credentials",
                project=project_name, invalid_fields=[label for label, _ in bad_keys],
            )
            continue

        try:
            env = load_native_env_file(env_path)
            missing = [k for k in (user_key, pass_key, db_key) if k not in env]
            if missing:
                log_event(
                    logger, logging.ERROR,
                    "native_postgres entry skipped: configured key name(s) not found in .env",
                    project=project_name, env_path=env_path, missing_keys=missing,
                )
                continue
            targets.append(
                PostgresTarget(
                    kind="native",
                    project_name=project_name,
                    host=entry.get("host", "localhost"),
                    port=int(entry.get("port", 5432)),
                    env_path=env_path,
                    pg_user=env.get(user_key),
                    pg_password=env.get(pass_key),
                    pg_db=env.get(db_key),
                )
            )
        except Exception as e:
            log_event(logger, logging.ERROR, "failed to load native target config",
                      project=project_name, env_path=env_path, error=str(e))
    return targets


# ---------------------------------------------------------------------------
# Dump
# ---------------------------------------------------------------------------


def dump_postgres(
    container_name: str,
    pg_user: str,
    pg_db: str,
    output_dir: Path,
    project_name: str,
    logger: logging.Logger,
    dry_run: bool = False,
) -> Optional[Path]:
    """
    Runs pg_dump *inside* the container and streams the output to a local
    gzip file. Using `docker exec` avoids exposing the DB over the host
    network and guarantees pg_dump's version always matches the server.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{project_name}_{pg_db}_{timestamp}.sql.gz"
    output_path = output_dir / filename

    if dry_run:
        log_event(
            logger, logging.INFO, "dry-run: would dump", container=container_name,
            db=pg_db, target_file=str(output_path),
        )
        return None

    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "docker", "exec", container_name,
        "pg_dump", "-U", pg_user, "-d", pg_db, "--no-password", "-F", "p",
    ]

    log_event(logger, logging.INFO, "starting pg_dump", container=container_name, db=pg_db)
    t0 = time.time()
    try:
        with open(output_path, "wb") as raw_out:
            with gzip.GzipFile(fileobj=raw_out, mode="wb") as gz_out:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                assert proc.stdout is not None
                shutil.copyfileobj(proc.stdout, gz_out)
                _, stderr = proc.communicate()
                if proc.returncode != 0:
                    raise RuntimeError(f"pg_dump exited {proc.returncode}: {stderr.decode(errors='replace')[:500]}")
    except Exception:
        output_path.unlink(missing_ok=True)
        raise

    elapsed = time.time() - t0
    size_mb = output_path.stat().st_size / (1024 * 1024)
    log_event(
        logger, logging.INFO, "pg_dump complete", container=container_name, db=pg_db,
        elapsed_sec=round(elapsed, 1), size_mb=round(size_mb, 2), file=str(output_path),
    )
    return output_path


def dump_postgres_native(
    host: str,
    port: int,
    pg_user: str,
    pg_password: Optional[str],
    pg_db: str,
    output_dir: Path,
    project_name: str,
    logger: logging.Logger,
    dry_run: bool = False,
) -> Optional[Path]:
    """
    Runs pg_dump directly on the host against a native (non-Docker) Postgres
    instance. Password is passed via the PGPASSWORD environment variable of
    the subprocess only — never as a CLI argument (which would be visible
    to other users via `ps aux`) and never logged.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{project_name}_{pg_db}_{timestamp}.sql.gz"
    output_path = output_dir / filename

    if dry_run:
        log_event(
            logger, logging.INFO, "dry-run: would dump (native)", project=project_name,
            host=host, port=port, db=pg_db, target_file=str(output_path),
        )
        return None

    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "pg_dump", "-h", host, "-p", str(port), "-U", pg_user, "-d", pg_db,
        "--no-password", "-F", "p",
    ]
    env = dict(os.environ)
    if pg_password:
        env["PGPASSWORD"] = pg_password

    log_event(logger, logging.INFO, "starting pg_dump (native)", project=project_name, host=host, db=pg_db)
    t0 = time.time()
    try:
        with open(output_path, "wb") as raw_out:
            with gzip.GzipFile(fileobj=raw_out, mode="wb") as gz_out:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                assert proc.stdout is not None
                shutil.copyfileobj(proc.stdout, gz_out)
                _, stderr = proc.communicate()
                if proc.returncode != 0:
                    raise RuntimeError(f"pg_dump exited {proc.returncode}: {stderr.decode(errors='replace')[:500]}")
    except Exception:
        output_path.unlink(missing_ok=True)
        raise

    elapsed = time.time() - t0
    size_mb = output_path.stat().st_size / (1024 * 1024)
    log_event(
        logger, logging.INFO, "pg_dump complete (native)", project=project_name, db=pg_db,
        elapsed_sec=round(elapsed, 1), size_mb=round(size_mb, 2), file=str(output_path),
    )
    return output_path


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def cleanup_local(backup_dir: Path, retention_hours: int, logger: logging.Logger, dry_run: bool):
    if not backup_dir.exists():
        return
    cutoff = time.time() - retention_hours * 3600
    for f in backup_dir.glob("*.sql.gz"):
        if f.stat().st_mtime < cutoff:
            log_event(logger, logging.INFO, "local retention: removing old backup", file=str(f), dry_run=dry_run)
            if not dry_run:
                f.unlink(missing_ok=True)


def cleanup_remote(
    uploader: SharePointUploader,
    remote_folder: str,
    retention_hours: int,
    logger: logging.Logger,
    dry_run: bool,
):
    try:
        items = uploader.list_folder(remote_folder)
    except SharePointUploadError as e:
        log_event(logger, logging.WARNING, "remote retention: list failed", folder=remote_folder, error=str(e))
        return

    cutoff = datetime.now(timezone.utc) - timedelta(hours=retention_hours)
    for item in items:
        created = item.get("createdDateTime")
        if not created:
            continue
        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if created_dt < cutoff:
            log_event(
                logger, logging.INFO, "remote retention: removing old backup",
                name=item.get("name"), folder=remote_folder, dry_run=dry_run,
            )
            if not dry_run:
                uploader.delete_item(item["id"])


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def notify_failure(config: dict, project_name: str, error: str, logger: logging.Logger):
    telegram_webhook = config.get("notifications", {}).get("telegram_webhook")
    slack_webhook = config.get("notifications", {}).get("slack_webhook")
    text = f"[auto-backup] FAILED: {project_name}\n{error[:500]}"

    for name, webhook, payload in (
        ("telegram", telegram_webhook, {"text": text}),
        ("slack", slack_webhook, {"text": text}),
    ):
        if not webhook:
            continue
        try:
            requests.post(webhook, json=payload, timeout=15)
        except Exception as e:
            log_event(logger, logging.WARNING, f"{name} notification failed", error=str(e))


# ---------------------------------------------------------------------------
# Config loading / validation
# ---------------------------------------------------------------------------


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


REQUIRED_CONFIG_KEYS = ["environment", "sharepoint", "backup_root", "retention_hours"]
REQUIRED_SHAREPOINT_KEYS = ["site_hostname", "site_path", "folder_prefix"]
REQUIRED_CRED_KEYS = ["tenant_id", "client_id", "client_secret"]


def validate_config(config: dict, credentials: dict) -> list[str]:
    errors = []
    for key in REQUIRED_CONFIG_KEYS:
        if key not in config:
            errors.append(f"config.json missing required key: {key}")
    for key in REQUIRED_SHAREPOINT_KEYS:
        if key not in config.get("sharepoint", {}):
            errors.append(f"config.json.sharepoint missing required key: {key}")
    azure = credentials.get("azure_ad", {})
    for key in REQUIRED_CRED_KEYS:
        if not azure.get(key) or "YOUR_" in str(azure.get(key)):
            errors.append(f"credentials.json.azure_ad missing or placeholder value for: {key}")

    try:
        subprocess.run(["docker", "ps"], capture_output=True, check=True)
    except Exception as e:
        errors.append(f"docker not accessible (Docker-based targets will be skipped): {e}")

    key_name_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    for entry in config.get("native_postgres", []):
        pname = entry.get("project_name", "<unnamed>")
        if "project_name" not in entry:
            errors.append("config.json native_postgres entry missing 'project_name'")
        env_path = entry.get("env_path")
        if not env_path:
            errors.append(f"native_postgres[{pname}] missing 'env_path'")
            continue
        if not Path(env_path).exists():
            errors.append(f"native_postgres[{pname}] env_path not found: {env_path}")
            continue

        for field_name, default in (
            ("user_key", "POSTGRES_USER"), ("password_key", "POSTGRES_PASSWORD"), ("db_key", "POSTGRES_DB")
        ):
            key_value = entry.get(field_name, default)
            if not key_name_re.match(key_value):
                errors.append(
                    f"native_postgres[{pname}].{field_name} = '{key_value}' does not look like a "
                    f".env variable name (expected e.g. 'POSTGRES_PASSWORD'). "
                    f"This field must be the VARIABLE NAME in the .env file, never the actual "
                    f"username/password/db value — config.json must never contain real credentials."
                )

        # Only check key presence in the .env if the key names themselves passed the format check.
        if not any(f"native_postgres[{pname}]" in e and "does not look like" in e for e in errors):
            try:
                env = load_native_env_file(env_path)
                for field_name, default in (
                    ("user_key", "POSTGRES_USER"), ("password_key", "POSTGRES_PASSWORD"), ("db_key", "POSTGRES_DB")
                ):
                    key_value = entry.get(field_name, default)
                    if key_value not in env:
                        errors.append(
                            f"native_postgres[{pname}].{field_name} = '{key_value}' not found as a "
                            f"variable in {env_path}"
                        )
            except Exception as e:
                errors.append(f"native_postgres[{pname}] failed to read env_path {env_path}: {e}")

    return errors


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def discover_all_targets(config: dict, logger: logging.Logger) -> list[PostgresTarget]:
    """
    Combines Docker-based and native (host-installed) discovery into one
    list. Docker discovery failing (e.g. Docker not installed on a
    DB-only host) does not prevent native targets from being backed up,
    and vice versa.
    """
    overrides = config.get("project_name_overrides", {})
    discovery_cfg = config.get("discovery", {})
    targets: list[PostgresTarget] = []

    try:
        docker_targets = discover_postgres_containers(
            image_patterns=discovery_cfg.get("postgres_image_patterns", ["postgres"]),
            exclude=discovery_cfg.get("exclude_containers", []),
        )
        for t in docker_targets:
            t.project_name = derive_project_name(t.container_name, overrides)
        targets.extend(docker_targets)
        log_event(logger, logging.INFO, "docker discovery complete", count=len(docker_targets),
                  targets=[t.container_name for t in docker_targets])
    except Exception as e:
        log_event(logger, logging.WARNING, "docker discovery failed, skipping docker targets", error=str(e))

    native_configs = config.get("native_postgres", [])
    if native_configs:
        configured_ports = {int(entry.get("port", 5432)) for entry in native_configs}
        if is_native_postgres_present(configured_ports):
            native_targets = build_native_targets(native_configs, logger)
            targets.extend(native_targets)
            log_event(logger, logging.INFO, "native discovery complete", count=len(native_targets),
                      targets=[t.project_name for t in native_targets])
        else:
            log_event(logger, logging.INFO,
                      "native_postgres configured but no native postgres service/port detected, skipping")

    return targets


def run(config: dict, credentials: dict, dry_run: bool, logger: logging.Logger):
    backup_root = Path(config["backup_root"])
    retention_hours = int(config["retention_hours"])
    environment = config["environment"]
    folder_prefix = config["sharepoint"]["folder_prefix"]

    targets = discover_all_targets(config, logger)

    if not targets:
        log_event(logger, logging.WARNING, "no postgres targets found (docker or native), nothing to do")
        return

    uploader: Optional[SharePointUploader] = None
    if not dry_run:
        sp_cfg = load_sharepoint_config(config, credentials)
        uploader = SharePointUploader(sp_cfg)

    failures = []

    for target in targets:
        try:
            if target.kind == "docker":
                env = get_container_env(target.container_name)
                pg_user = env.get("POSTGRES_USER", "postgres")
                pg_db = env.get("POSTGRES_DB", pg_user)

                local_dir = backup_root / target.project_name
                dump_path = dump_postgres(
                    container_name=target.container_name,
                    pg_user=pg_user,
                    pg_db=pg_db,
                    output_dir=local_dir,
                    project_name=target.project_name,
                    logger=logger,
                    dry_run=dry_run,
                )
            elif target.kind == "native":
                if not target.pg_user or not target.pg_db:
                    raise RuntimeError(
                        f"native target '{target.project_name}' missing user/db "
                        f"(check env_path={target.env_path} and user_key/db_key in config)"
                    )
                local_dir = backup_root / target.project_name
                dump_path = dump_postgres_native(
                    host=target.host,
                    port=target.port,
                    pg_user=target.pg_user,
                    pg_password=target.pg_password,
                    pg_db=target.pg_db,
                    output_dir=local_dir,
                    project_name=target.project_name,
                    logger=logger,
                    dry_run=dry_run,
                )
            else:
                raise RuntimeError(f"unknown target kind: {target.kind}")

            remote_folder = f"{folder_prefix}/{environment}/{target.project_name}"

            if dry_run:
                log_event(logger, logging.INFO, "dry-run: would upload",
                          project=target.project_name, remote_folder=remote_folder)
            else:
                assert uploader is not None and dump_path is not None
                web_url = uploader.upload_file(dump_path, remote_folder)
                log_event(logger, logging.INFO, "upload complete",
                          project=target.project_name, web_url=web_url)

            cleanup_local(local_dir, retention_hours, logger, dry_run)
            if uploader:
                cleanup_remote(uploader, remote_folder, retention_hours, logger, dry_run)

        except Exception as e:
            log_event(logger, logging.ERROR, "backup failed for target",
                      project=target.project_name, kind=target.kind, error=str(e))
            failures.append((target.project_name, str(e)))
            notify_failure(config, target.project_name, str(e), logger)

    if failures:
        log_event(logger, logging.ERROR, "backup run finished with failures",
                  failed_count=len(failures), total=len(targets))
        sys.exit(1)
    else:
        log_event(logger, logging.INFO, "backup run finished successfully", total=len(targets))


def main():
    parser = argparse.ArgumentParser(description="Auto-discovering PostgreSQL backup framework")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--credentials", default="credentials.json")
    parser.add_argument("--dry-run", action="store_true", help="Discover and log actions without writing/uploading")
    parser.add_argument("--validate-config", action="store_true", help="Validate config/credentials and exit")
    args = parser.parse_args()

    try:
        config = load_json(args.config)
    except FileNotFoundError:
        print(f"Config file not found: {args.config}", file=sys.stderr)
        sys.exit(2)

    try:
        credentials = load_json(args.credentials)
    except FileNotFoundError:
        print(f"Credentials file not found: {args.credentials}", file=sys.stderr)
        sys.exit(2)

    if args.validate_config:
        errors = validate_config(config, credentials)
        if errors:
            for e in errors:
                print(f"INVALID: {e}", file=sys.stderr)
            sys.exit(1)
        print("Config and credentials look valid.")
        sys.exit(0)

    logger = setup_logging(config.get("log_path"))
    run(config, credentials, dry_run=args.dry_run, logger=logger)


if __name__ == "__main__":
    main()
