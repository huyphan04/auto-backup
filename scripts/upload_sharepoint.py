#!/usr/bin/env python3
"""
Upload a single file to a SharePoint document library via Microsoft Graph API,
authenticating with an Azure AD App Registration (client credentials flow).

Usage:
    upload_sharepoint.py <path-to-file>
"""

import os
import sys
import time
import logging
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR.parent / ".env"


def load_env(path: Path) -> None:
    """Minimal .env loader (no external deps required)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


load_env(ENV_FILE)

LOG_FILE = os.environ.get("LOG_FILE", "/var/log/auto-backup.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [upload] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("upload_sharepoint")

TENANT_ID = os.environ["TENANT_ID"]
CLIENT_ID = os.environ["CLIENT_ID"]
CLIENT_SECRET = os.environ["CLIENT_SECRET"]
SHAREPOINT_HOSTNAME = os.environ.get("SHAREPOINT_HOSTNAME", "havasgroupvncom.sharepoint.com")
SITE_PATH = os.environ.get("SHAREPOINT_SITE_PATH", "/sites/BACKUP_PRD")
TARGET_FOLDER = os.environ.get("SHAREPOINT_FOLDER", "HDBANK/PRD")

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
LARGE_FILE_THRESHOLD = 4 * 1024 * 1024   # Graph's simple-upload limit
CHUNK_SIZE = 5 * 1024 * 1024             # must be a multiple of 320 KiB


def get_access_token() -> str:
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }
    resp = requests.post(url, data=data, timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_drive_id(token: str) -> str:
    headers = {"Authorization": f"Bearer {token}"}
    site_url = f"{GRAPH_ROOT}/sites/{SHAREPOINT_HOSTNAME}:{SITE_PATH}"
    resp = requests.get(site_url, headers=headers, timeout=30)
    resp.raise_for_status()
    site_id = resp.json()["id"]

    drive_url = f"{GRAPH_ROOT}/sites/{site_id}/drive"
    resp = requests.get(drive_url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def upload_small_file(token: str, drive_id: str, remote_path: str, file_path: Path) -> dict:
    url = f"{GRAPH_ROOT}/drives/{drive_id}/root:/{remote_path}:/content"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
    }
    with open(file_path, "rb") as f:
        resp = requests.put(url, headers=headers, data=f, timeout=120)
    resp.raise_for_status()
    return resp.json()


def upload_large_file(token: str, drive_id: str, remote_path: str, file_path: Path) -> dict:
    session_url = f"{GRAPH_ROOT}/drives/{drive_id}/root:/{remote_path}:/createUploadSession"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(
        session_url,
        headers=headers,
        json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        timeout=30,
    )
    resp.raise_for_status()
    upload_url = resp.json()["uploadUrl"]

    file_size = file_path.stat().st_size
    result = {}
    with open(file_path, "rb") as f:
        start = 0
        while start < file_size:
            chunk = f.read(CHUNK_SIZE)
            end = start + len(chunk) - 1
            chunk_headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end}/{file_size}",
            }
            resp = requests.put(upload_url, headers=chunk_headers, data=chunk, timeout=180)
            resp.raise_for_status()
            start += len(chunk)
            result = resp.json() if resp.content else result
    return result


def upload_with_retry(drive_id: str, remote_path: str, file_path: Path, max_retries: int = 3) -> dict:
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            token = get_access_token()
            size = file_path.stat().st_size
            if size > LARGE_FILE_THRESHOLD:
                return upload_large_file(token, drive_id, remote_path, file_path)
            return upload_small_file(token, drive_id, remote_path, file_path)
        except requests.HTTPError as e:
            last_error = e
            logger.warning("Upload attempt %d/%d failed: %s", attempt, max_retries, e)
            time.sleep(5 * attempt)
    raise last_error


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: upload_sharepoint.py <path-to-file>", file=sys.stderr)
        sys.exit(1)

    file_path = Path(sys.argv[1]).resolve()
    if not file_path.exists():
        logger.error("File not found: %s", file_path)
        sys.exit(1)

    remote_path = f"{TARGET_FOLDER.strip('/')}/{file_path.name}"

    try:
        token = get_access_token()
        drive_id = get_drive_id(token)
        result = upload_with_retry(drive_id, remote_path, file_path)
        logger.info(
            "Uploaded %s -> %s (%s bytes)",
            file_path.name, remote_path, file_path.stat().st_size,
        )
        print(result.get("webUrl", "uploaded"))
    except Exception as exc:
        logger.error("Upload failed for %s: %s", file_path.name, exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
