#!/usr/bin/env bash
# Orchestrator: dump PostgreSQL -> upload to SharePoint.
# Invoked by auto-backup.service (triggered by auto-backup.timer every 2h).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

LOG_FILE="${LOG_FILE:-/var/log/auto-backup.log}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [run] $1" >> "$LOG_FILE"; }

log "=== Backup cycle started ==="

DUMP_FILE=$("${SCRIPT_DIR}/backup_postgres.sh")

if [[ -z "$DUMP_FILE" || ! -f "$DUMP_FILE" ]]; then
  log "ERROR: backup script did not produce a valid dump file"
  exit 1
fi

log "Dump created: $DUMP_FILE, uploading to SharePoint..."

if python3 "${SCRIPT_DIR}/upload_sharepoint.py" "$DUMP_FILE"; then
  log "Upload succeeded for $DUMP_FILE"
else
  log "ERROR: upload failed for $DUMP_FILE (file kept locally for the next retry)"
  exit 1
fi

log "=== Backup cycle finished ==="
