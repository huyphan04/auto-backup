#!/usr/bin/env bash
#
# uninstall.sh — removes everything install.sh created:
#   - auto-backup.timer / auto-backup.service (stopped, disabled, deleted)
#   - /opt/auto-backup (code + config.json + credentials.json)
#
# By default this does NOT touch:
#   - backup_root (locally staged .sql.gz files)
#   - log_path (backup.jsonl)
#   - anything on SharePoint
# because those are data, not installed program files. Pass --purge-data
# to also remove the local backup_root and log directory read from
# config.json before it's deleted.
#
# Usage:
#   sudo ./uninstall.sh              # remove service/timer/code/config only
#   sudo ./uninstall.sh --purge-data # also delete local backup files + logs

set -euo pipefail

INSTALL_DIR="/opt/auto-backup"
SERVICE_NAME="auto-backup"
PURGE_DATA=false

for arg in "$@"; do
  case "$arg" in
    --purge-data) PURGE_DATA=true ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root (sudo ./uninstall.sh)" >&2
  exit 1
fi

echo "==> Stopping and disabling timer/service"
systemctl stop "${SERVICE_NAME}.timer" 2>/dev/null || true
systemctl stop "${SERVICE_NAME}.service" 2>/dev/null || true
systemctl disable "${SERVICE_NAME}.timer" 2>/dev/null || true

echo "==> Removing systemd unit files"
rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
rm -f "/etc/systemd/system/${SERVICE_NAME}.timer"
systemctl daemon-reload
systemctl reset-failed "${SERVICE_NAME}.service" 2>/dev/null || true
systemctl reset-failed "${SERVICE_NAME}.timer" 2>/dev/null || true

if [[ "$PURGE_DATA" == "true" && -f "$INSTALL_DIR/config.json" ]]; then
  BACKUP_ROOT=$(python3 -c "import json; print(json.load(open('$INSTALL_DIR/config.json')).get('backup_root',''))" 2>/dev/null || echo "")
  LOG_PATH=$(python3 -c "import json; print(json.load(open('$INSTALL_DIR/config.json')).get('log_path',''))" 2>/dev/null || echo "")

  if [[ -n "$BACKUP_ROOT" && -d "$BACKUP_ROOT" ]]; then
    echo "==> --purge-data: removing local backup files at $BACKUP_ROOT"
    rm -rf "$BACKUP_ROOT"
  fi
  if [[ -n "$LOG_PATH" ]]; then
    LOG_DIR=$(dirname "$LOG_PATH")
    if [[ -d "$LOG_DIR" ]]; then
      echo "==> --purge-data: removing log directory at $LOG_DIR"
      rm -rf "$LOG_DIR"
    fi
  fi
elif [[ "$PURGE_DATA" == "true" ]]; then
  echo "==> --purge-data requested but $INSTALL_DIR/config.json not found, skipping data removal"
fi

echo "==> Removing $INSTALL_DIR (code, config.json, credentials.json)"
rm -rf "$INSTALL_DIR"

echo ""
echo "Done. Removed:"
echo "  - systemd: ${SERVICE_NAME}.timer, ${SERVICE_NAME}.service"
echo "  - $INSTALL_DIR (including config.json and credentials.json)"
if [[ "$PURGE_DATA" == "true" ]]; then
  echo "  - local backup_root and log directory (--purge-data)"
else
  echo ""
  echo "Local backup files and logs were left in place."
  echo "Re-run with --purge-data to delete those too:"
  echo "  sudo ./uninstall.sh --purge-data"
fi
echo ""
echo "Note: this does not delete anything already uploaded to SharePoint."
