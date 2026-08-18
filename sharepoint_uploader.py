"""
sharepoint_uploader.py

Microsoft Graph API client for uploading backup files to SharePoint.
Uses Azure AD client-credentials (app-only) OAuth2 flow.

Design notes:
- Small files (<4MB) use a single PUT.
- Large files use an upload session (resumable, chunked) per Graph API docs.
- Never logs the client secret or access token.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("auto_backup.sharepoint")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB, must be a multiple of 320 KiB per Graph docs
SMALL_FILE_THRESHOLD = 4 * 1024 * 1024  # 4 MB


class SharePointAuthError(RuntimeError):
    pass


class SharePointUploadError(RuntimeError):
    pass


@dataclass
class SharePointConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    site_hostname: str  # e.g. "havasgroupvncom.sharepoint.com"
    site_path: str      # e.g. "/sites/BACKUP_PRD"


class SharePointUploader:
    def __init__(self, cfg: SharePointConfig, timeout: int = 60):
        self._cfg = cfg
        self._timeout = timeout
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._site_id: Optional[str] = None
        self._drive_id: Optional[str] = None

    # ---------- Auth ----------

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token

        url = f"https://login.microsoftonline.com/{self._cfg.tenant_id}/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self._cfg.client_id,
            "client_secret": self._cfg.client_secret,
            "scope": "https://graph.microsoft.com/.default",
        }
        resp = requests.post(url, data=data, timeout=self._timeout)
        if resp.status_code != 200:
            # Deliberately do not include request body (contains secret) in the error.
            raise SharePointAuthError(
                f"Azure AD token request failed: HTTP {resp.status_code} {resp.text[:300]}"
            )
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = time.time() + int(payload.get("expires_in", 3600))
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}"}

    # ---------- Site / drive resolution ----------

    def _resolve_site_id(self) -> str:
        if self._site_id:
            return self._site_id
        url = f"{GRAPH_BASE}/sites/{self._cfg.site_hostname}:{self._cfg.site_path}"
        resp = requests.get(url, headers=self._headers(), timeout=self._timeout)
        if resp.status_code != 200:
            raise SharePointAuthError(
                f"Failed to resolve SharePoint site: HTTP {resp.status_code} {resp.text[:300]}"
            )
        self._site_id = resp.json()["id"]
        return self._site_id

    def _resolve_drive_id(self) -> str:
        if self._drive_id:
            return self._drive_id
        site_id = self._resolve_site_id()
        url = f"{GRAPH_BASE}/sites/{site_id}/drive"
        resp = requests.get(url, headers=self._headers(), timeout=self._timeout)
        if resp.status_code != 200:
            raise SharePointAuthError(
                f"Failed to resolve default drive: HTTP {resp.status_code} {resp.text[:300]}"
            )
        self._drive_id = resp.json()["id"]
        return self._drive_id

    # ---------- Upload ----------

    def upload_file(self, local_path: Path, remote_folder: str) -> str:
        """
        Uploads local_path into remote_folder (path relative to the drive root,
        e.g. "HDBANK/PRD/kolbrancherxcom-db-1"). Creates intermediate folders
        implicitly (Graph API does this for you on PUT-by-path).

        Returns the webUrl of the uploaded item.
        """
        size = local_path.stat().st_size
        remote_folder = remote_folder.strip("/")
        remote_item_path = f"{remote_folder}/{local_path.name}"

        if size <= SMALL_FILE_THRESHOLD:
            return self._upload_small(local_path, remote_item_path)
        return self._upload_large(local_path, remote_item_path, size)

    def _drive_item_url(self, remote_item_path: str) -> str:
        drive_id = self._resolve_drive_id()
        return f"{GRAPH_BASE}/drives/{drive_id}/root:/{remote_item_path}:"

    def _upload_small(self, local_path: Path, remote_item_path: str) -> str:
        url = f"{self._drive_item_url(remote_item_path)}/content"
        with open(local_path, "rb") as fh:
            resp = requests.put(
                url,
                headers={**self._headers(), "Content-Type": "application/octet-stream"},
                data=fh,
                timeout=self._timeout,
            )
        if resp.status_code not in (200, 201):
            raise SharePointUploadError(
                f"Small-file upload failed for {local_path.name}: "
                f"HTTP {resp.status_code} {resp.text[:300]}"
            )
        return resp.json().get("webUrl", "")

    def _upload_large(self, local_path: Path, remote_item_path: str, size: int) -> str:
        session_url = f"{self._drive_item_url(remote_item_path)}/createUploadSession"
        resp = requests.post(
            session_url,
            headers=self._headers(),
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
            timeout=self._timeout,
        )
        if resp.status_code != 200:
            raise SharePointUploadError(
                f"Failed to create upload session for {local_path.name}: "
                f"HTTP {resp.status_code} {resp.text[:300]}"
            )
        upload_url = resp.json()["uploadUrl"]

        with open(local_path, "rb") as fh:
            offset = 0
            while offset < size:
                chunk = fh.read(CHUNK_SIZE)
                chunk_len = len(chunk)
                end = offset + chunk_len - 1
                headers = {
                    "Content-Length": str(chunk_len),
                    "Content-Range": f"bytes {offset}-{end}/{size}",
                }
                # Upload session URLs are pre-authenticated; do NOT send the bearer token.
                put_resp = requests.put(
                    upload_url, headers=headers, data=chunk, timeout=self._timeout
                )
                if put_resp.status_code not in (200, 201, 202):
                    raise SharePointUploadError(
                        f"Chunk upload failed for {local_path.name} at offset {offset}: "
                        f"HTTP {put_resp.status_code} {put_resp.text[:300]}"
                    )
                offset += chunk_len
                if put_resp.status_code in (200, 201):
                    return put_resp.json().get("webUrl", "")

        raise SharePointUploadError(f"Upload session for {local_path.name} ended without completion")

    # ---------- Retention (remote) ----------

    def list_folder(self, remote_folder: str) -> list[dict]:
        drive_id = self._resolve_drive_id()
        remote_folder = remote_folder.strip("/")
        url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{remote_folder}:/children"
        items = []
        while url:
            resp = requests.get(url, headers=self._headers(), timeout=self._timeout)
            if resp.status_code == 404:
                return []
            if resp.status_code != 200:
                raise SharePointUploadError(
                    f"Failed to list folder {remote_folder}: HTTP {resp.status_code} {resp.text[:300]}"
                )
            body = resp.json()
            items.extend(body.get("value", []))
            url = body.get("@odata.nextLink")
        return items

    def delete_item(self, item_id: str) -> None:
        drive_id = self._resolve_drive_id()
        url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}"
        resp = requests.delete(url, headers=self._headers(), timeout=self._timeout)
        if resp.status_code not in (204, 404):
            raise SharePointUploadError(
                f"Failed to delete remote item {item_id}: HTTP {resp.status_code} {resp.text[:300]}"
            )


def load_sharepoint_config(config: dict, credentials: dict) -> SharePointConfig:
    return SharePointConfig(
        tenant_id=credentials["azure_ad"]["tenant_id"],
        client_id=credentials["azure_ad"]["client_id"],
        client_secret=credentials["azure_ad"]["client_secret"],
        site_hostname=config["sharepoint"]["site_hostname"],
        site_path=config["sharepoint"]["site_path"],
    )
