#!/usr/bin/env bash
# Dump the PostgreSQL database, gzip it, write to BACKUP_DIR,
# and clean up local dumps older than RETENTION_HOURS.
# Prints the full path of the newly created dump file to stdout.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

LOG_FILE="${LOG_FILE:-/var/log/scorex-backup.log}"
BACKUP_DIR="${BACKUP_DIR:-/opt/scorex-backup/dumps}"
RETENTION_HOURS="${RETENTION_HOURS:-24}"
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
DUMP_FILE="${BACKUP_DIR}/${POSTGRES_DATABASE}_${TIMESTAMP}.sql.gz"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] [dump] $1" >> "$LOG_FILE"
}

mkdir -p "$BACKUP_DIR"

log "Starting backup of database '${POSTGRES_DATABASE}'"

export PGPASSWORD="${POSTGRES_PASS}"

PG_DUMP_BIN="${PG_DUMP_BIN:-pg_dump}"

if "$PG_DUMP_BIN" -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U "${POSTGRES_USER}" -d "${POSTGRES_DATABASE}" \
    | gzip > "${DUMP_FILE}.tmp"; then
  mv "${DUMP_FILE}.tmp" "${DUMP_FILE}"
  SIZE=$(du -h "${DUMP_FILE}" | cut -f1)
  log "Backup completed successfully: ${DUMP_FILE} (${SIZE})"
else
  rm -f "${DUMP_FILE}.tmp"
  log "ERROR: pg_dump failed"
  unset PGPASSWORD
  exit 1
fi

unset PGPASSWORD

# Clean up local dumps older than RETENTION_HOURS (they should already be
# safely on SharePoint by then)
find "$BACKUP_DIR" -name "*.sql.gz" -type f -mmin "+$((RETENTION_HOURS * 60))" -print0 \
  | while IFS= read -r -d '' f; do
      rm -f "$f"
      log "Removed old local backup: $f"
    done

# Only the dump path goes to stdout, everything else goes to the log file
echo "$DUMP_FILE"
