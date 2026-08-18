# auto-backup

Auto-discovering PostgreSQL backup framework. Finds Postgres wherever it's
running — **inside Docker containers** or **installed natively on the
host** — dumps it, uploads to SharePoint via Microsoft Graph API, and
cleans up old backups (local + remote) by retention.

No manual container list to maintain for the Docker side — add a new
Postgres container to the host and it gets picked up on the next hourly
run automatically.

## How it works

### Discovery — two independent sources, both run every time

**Docker-based:**
1. `docker ps` → filter containers whose image contains `postgres`
   (configurable pattern).
2. Derive a project name from the container name by stripping common
   compose suffixes (`-db-1`, `-1`, etc.), or use an explicit override in
   `config.json` if a container doesn't follow that convention.
3. Read `POSTGRES_USER` / `POSTGRES_DB` directly from the container's own
   environment via `docker exec <container> env` — nothing to duplicate in
   a config file, no DB passwords stored on disk for Docker targets.
4. Dump with `docker exec <container> pg_dump ...` streamed straight into a
   local gzip file. Runs the container's own `pg_dump`, so there is never a
   client/server version mismatch, and the DB is never exposed over the
   host network for backup purposes.

**Native (host-installed, non-Docker) Postgres:**
1. Only looked at if `native_postgres` has entries in `config.json` — each
   entry names a project explicitly (there's no container name to infer
   from).
2. Presence check before touching anything: is a `postgresql*` systemd
   service running, OR is the port that entry configured actually
   listening? If neither, that entry is skipped for this run rather than
   failing — a project that isn't up yet doesn't break the whole backup job.
3. Credentials are read live from that project's own `.env` file (path you
   give in `config.json`) — never copied into the backup config. Field
   names are configurable per entry (`user_key`/`password_key`/`db_key`)
   since projects don't all use `POSTGRES_PASSWORD`/`POSTGRES_DB` — e.g. one
   `.env` might use `POSTGRES_PASS` / `POSTGRES_DATABASE` instead.
4. Dump with `pg_dump` run directly on the host, against `host:port`. The
   password is passed via the `PGPASSWORD` environment variable of the
   subprocess only — never as a CLI argument (visible to other users via
   `ps aux`) and never logged.

Both kinds converge into the same pipeline after the dump step:

- **Upload**: Microsoft Graph API, app-only OAuth2 (client credentials).
  Files >4MB use a resumable upload session.
- **Retention**: deletes local and remote backups older than
  `retention_hours`, per project folder.
- **Notify**: on failure, posts to Telegram/Slack webhook if configured.

A failure or absence in one source (e.g. Docker not installed on a
DB-only host, or a native project not configured) never blocks the other
source from running.

## Setup

```bash
cp config.example.json config.json
cp credentials.example.json credentials.json
# edit both files with real values — never commit credentials.json
```

`config.json` — no secrets, safe to keep in git (with `credentials.json`
gitignored):

| Key | Meaning |
|---|---|
| `environment` | `"PRD"` or `"DEV"` — becomes part of the SharePoint folder path |
| `sharepoint.site_hostname` | e.g. `havasgroupvncom.sharepoint.com` |
| `sharepoint.site_path` | e.g. `/sites/BACKUP_PRD` |
| `sharepoint.folder_prefix` | top folder, e.g. `HDBANK` — final path becomes `HDBANK/<environment>/<project>/` |
| `discovery.postgres_image_patterns` | substrings to match against image name (default `["postgres"]`) |
| `discovery.exclude_containers` | container names to skip |
| `project_name_overrides` | map container name → display name, for containers that don't follow the `<project>-db-1` naming convention |
| `native_postgres` | list of host-installed Postgres targets — see below |
| `retention_hours` | how long to keep backups, local and remote |
| `backup_root` | local staging directory |
| `log_path` | JSON-lines log file |
| `notifications.telegram_webhook` / `.slack_webhook` | optional failure alerts |

`credentials.json` — **secrets, chmod 600, never commit**:

```json
{
  "azure_ad": {
    "tenant_id": "...",
    "client_id": "...",
    "client_secret": "..."
  }
}
```

The Azure AD App Registration needs `Sites.ReadWrite.All` (application
permission, admin-consented) on Microsoft Graph to write to the SharePoint
document library.

### `native_postgres` entries

For each Postgres instance installed directly on the host (not in Docker),
add one entry:

```json
{
  "project_name": "my-project",
  "env_path": "/opt/my-project/.env",
  "host": "localhost",
  "port": 5432,
  "user_key": "POSTGRES_USER",
  "password_key": "POSTGRES_PASSWORD",
  "db_key": "POSTGRES_DB"
}
```

`user_key` / `password_key` / `db_key` tell the tool which **variable
names** to read out of that project's `.env` — default to `POSTGRES_USER` /
`POSTGRES_PASSWORD` / `POSTGRES_DB` if omitted, override per-project since
not every `.env` uses those exact names (e.g. some use `POSTGRES_PASS` /
`POSTGRES_DATABASE` instead).

**These three fields must always be variable names, never actual
credentials.** `config.json` should never contain a real username or
password — the tool reads the live value out of the `.env` file at backup
time instead. `--validate-config` enforces this: it rejects any `*_key`
value that doesn't look like an env-var name (`UPPER_SNAKE_CASE`), and
separately checks that the variable name you gave actually exists in the
target `.env` file.

Note: the built-in `.env` parser reads plain `KEY=VALUE` lines and does not
expand `${VAR}` references. If a project's `.env` only defines a composed
value like `DB_DSN=host=${POSTGRES_HOST} ... password=${POSTGRES_PASSWORD} ...`,
point `user_key`/`password_key`/`db_key` at the underlying leaf variables,
not the composed DSN string.

If neither a running `postgresql*` systemd service nor the configured port
is detected, that entry is skipped for the run (logged, not treated as a
failure) — useful when a project isn't deployed on every host this tool
runs on.

## Usage

```bash
# Validate config/credentials without touching anything
python3 backup.py --validate-config

# See exactly what would happen — no dump, no upload, no deletes
python3 backup.py --dry-run

# Real run
python3 backup.py --config config.json --credentials credentials.json
```

## Install as hourly systemd timer

```bash
sudo ./install.sh
```

This installs to `/opt/auto-backup`, sets `chmod 600` on `credentials.json`,
pre-creates the backup/log directories with correct ownership (avoids the
classic `PermissionError` at first run), and enables an hourly
`auto-backup.timer`.

```bash
systemctl status auto-backup.timer      # confirm schedule
systemctl start auto-backup.service     # trigger a run immediately
journalctl -u auto-backup.service -f    # tail systemd logs
tail -f /var/log/auto-backup/backup.jsonl
```

## Uninstall

```bash
sudo ./uninstall.sh
```

Removes everything `install.sh` created: stops and disables
`auto-backup.timer`/`auto-backup.service`, deletes the systemd unit files,
and removes `/opt/auto-backup` (code, `config.json`, `credentials.json`).

By default it leaves local backup files (`backup_root`) and logs
(`log_path`) in place, since those are data, not installed program files.
To also delete those:

```bash
sudo ./uninstall.sh --purge-data
```

Nothing already uploaded to SharePoint is touched by either command — that
has to be cleaned up in SharePoint directly if you want it gone.

Safe to run even if install never completed or was already run once —
missing units/files/directories are skipped rather than causing an error.

## Adding a project that doesn't fit the naming convention

If a container's name isn't `<project>-db-1` (e.g. a legacy `postgresdb`
container), add an explicit mapping — no code changes needed:

```json
"project_name_overrides": {
  "postgresdb": "some-clearer-name"
}
```

## Security notes

- `credentials.json` must be `chmod 600`, owned by the service user.
- DB credentials are never stored on disk by this tool — read live from the
  container at backup time.
- If any secret (client secret, DB password, SharePoint account password)
  is ever pasted into a chat, ticket, or committed to git in plaintext,
  rotate it immediately — treat it as compromised regardless of whether it
  was actually misused.

## Roadmap (not yet implemented)

- MySQL/MariaDB driver (same discovery pattern, different `pg_dump` → `mysqldump`)
- MongoDB driver (`mongodump`)
- Oracle DB (`expdp`, notably more complex)
- Kubernetes executor (discover pods instead of containers)
- Periodic automated test-restore pipeline
