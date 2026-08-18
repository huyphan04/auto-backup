#!/usr/bin/env bash
#
# install.sh — installs the auto-backup framework as a systemd service+timer.
#
# Usage:
#   sudo ./install.sh
#
# Assumes config.json and credentials.json already exist in this directory
# (copy from the .example.json files and fill in real values first).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/auto-backup"
SERVICE_NAME="auto-backup"
RUN_USER="root"   # must be a user in the `docker` group to run `docker exec`/`docker ps`

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root (sudo ./install.sh)" >&2
  exit 1
fi

for f in config.json credentials.json; do
  if [[ ! -f "$SCRIPT_DIR/$f" ]]; then
    echo "Missing $SCRIPT_DIR/$f — copy from ${f%.json}.example.json and fill in real values first." >&2
    exit 1
  fi
done

echo "==> Installing files to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/backup.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/sharepoint_uploader.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/config.json" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/credentials.json" "$INSTALL_DIR/"

echo "==> Restricting credentials.json permissions (chmod 600)"
chmod 600 "$INSTALL_DIR/credentials.json"
chown "$RUN_USER:$RUN_USER" "$INSTALL_DIR/credentials.json"

echo "==> Installing Python dependencies"
pip3 install --break-system-packages -q requests

BACKUP_ROOT=$(python3 -c "import json; print(json.load(open('$INSTALL_DIR/config.json'))['backup_root'])")
LOG_PATH=$(python3 -c "import json; print(json.load(open('$INSTALL_DIR/config.json')).get('log_path', ''))")

echo "==> Pre-creating backup and log directories with correct ownership"
mkdir -p "$BACKUP_ROOT"
chown "$RUN_USER:$RUN_USER" "$BACKUP_ROOT"
if [[ -n "$LOG_PATH" ]]; then
  LOG_DIR=$(dirname "$LOG_PATH")
  mkdir -p "$LOG_DIR"
  touch "$LOG_PATH"
  chown -R "$RUN_USER:$RUN_USER" "$LOG_DIR"
fi

echo "==> Writing systemd service unit"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Auto-discovering PostgreSQL backup (auto-backup)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=${RUN_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/backup.py --config ${INSTALL_DIR}/config.json --credentials ${INSTALL_DIR}/credentials.json
EOF

echo "==> Writing systemd timer unit (hourly)"
cat > "/etc/systemd/system/${SERVICE_NAME}.timer" <<EOF
[Unit]
Description=Run auto-backup hourly

[Timer]
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
EOF

echo "==> Reloading systemd and enabling timer"
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.timer"

echo ""
echo "Done. Useful commands:"
echo "  systemctl status ${SERVICE_NAME}.timer     # check schedule"
echo "  systemctl start ${SERVICE_NAME}.service    # run a backup right now"
echo "  journalctl -u ${SERVICE_NAME}.service -f   # tail logs"
echo "  tail -f ${LOG_PATH:-<no log_path configured>}"
echo ""
echo "Before relying on this, run a dry run first:"
echo "  python3 ${INSTALL_DIR}/backup.py --config ${INSTALL_DIR}/config.json --credentials ${INSTALL_DIR}/credentials.json --dry-run"
echo "And validate config:"
echo "  python3 ${INSTALL_DIR}/backup.py --validate-config --config ${INSTALL_DIR}/config.json --credentials ${INSTALL_DIR}/credentials.json"
