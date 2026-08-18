#!/usr/bin/env bash
# Run as root: sudo ./install.sh
set -euo pipefail

INSTALL_DIR="/opt/auto-backup"
LOG_FILE="/var/log/auto-backup.log"
SERVICE_USER="backup_user"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Please run this script as root (sudo ./install.sh)" >&2
  exit 1
fi

echo ">> Creating service user (if needed)..."
if ! id "$SERVICE_USER" &>/dev/null; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo ">> Creating directories..."
mkdir -p "$INSTALL_DIR/scripts" "$INSTALL_DIR/dumps"

# IMPORTANT: create the log file and set permissions BEFORE the service
# ever runs as backup_user, otherwise the first run fails with a
# PermissionError trying to open a log file that doesn't exist / isn't
# writable by that user.
echo ">> Creating log file with correct permissions..."
touch "$LOG_FILE"
chown "$SERVICE_USER:$SERVICE_USER" "$LOG_FILE"
chmod 664 "$LOG_FILE"

echo ">> Copying files..."
cp "$SRC_DIR/scripts/"*.sh "$INSTALL_DIR/scripts/"
cp "$SRC_DIR/scripts/"*.py "$INSTALL_DIR/scripts/"
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  cp "$SRC_DIR/.env.example" "$INSTALL_DIR/.env"
else
  echo "   (existing $INSTALL_DIR/.env kept as-is)"
fi
chmod +x "$INSTALL_DIR/scripts/"*.sh
chmod 600 "$INSTALL_DIR/.env"

echo ">> Setting ownership..."
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo ">> Installing Python dependency (requests)..."
if ! python3 -c "import requests" &>/dev/null; then
  pip3 install --quiet requests || apt-get install -y python3-requests
fi

echo ">> Installing systemd units..."
cp "$SRC_DIR/systemd/auto-backup.service" /etc/systemd/system/
cp "$SRC_DIR/systemd/auto-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now auto-backup.timer

echo
echo ">> Done."
echo "   Check timer status : systemctl status auto-backup.timer"
echo "   Check next run time: systemctl list-timers auto-backup.timer"
echo "   Test run manually  : sudo -u $SERVICE_USER $INSTALL_DIR/scripts/run_backup.sh"
echo "   Watch logs         : tail -f $LOG_FILE"
