#!/usr/bin/env bash
# Run as root: sudo ./install.sh [project_name]
#
# The service/timer/install-dir/log-file names are all derived from
# PROJECT_NAME. You can either:
#   1. Set PROJECT_NAME in .env before running, or
#   2. Pass it as the first argument: sudo ./install.sh hdbank-crm
# The argument takes priority over .env.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Please run this script as root (sudo ./install.sh)" >&2
  exit 1
fi

# --- Resolve PROJECT_NAME (arg > existing deployed .env > local .env.example) ---
CLI_PROJECT_NAME="${1:-}"

read_env_var() {
  # $1 = var name, $2 = file path
  [[ -f "$2" ]] || return 1
  grep -E "^${1}=" "$2" | tail -n1 | cut -d'=' -f2-
}

if [[ -n "$CLI_PROJECT_NAME" ]]; then
  PROJECT_NAME="$CLI_PROJECT_NAME"
elif [[ -f "$SRC_DIR/.env" ]]; then
  PROJECT_NAME="$(read_env_var PROJECT_NAME "$SRC_DIR/.env")"
else
  PROJECT_NAME="$(read_env_var PROJECT_NAME "$SRC_DIR/.env.example")"
fi

if [[ -z "${PROJECT_NAME:-}" ]]; then
  echo "ERROR: could not determine PROJECT_NAME. Set it in .env or pass as argument." >&2
  exit 1
fi

INSTALL_DIR="/opt/${PROJECT_NAME}-backup"
LOG_FILE="/var/log/${PROJECT_NAME}-backup.log"
SERVICE_NAME="${PROJECT_NAME}-backup"

echo ">> Project: $PROJECT_NAME"
echo ">> Install dir: $INSTALL_DIR"
echo ">> Log file: $LOG_FILE"
echo ">> Service: ${SERVICE_NAME}.service / ${SERVICE_NAME}.timer"
echo

# --- Service user (shared across projects unless BACKUP_USER overridden) ---
if [[ -f "$SRC_DIR/.env" ]]; then
  SERVICE_USER="$(read_env_var BACKUP_USER "$SRC_DIR/.env")"
else
  SERVICE_USER="$(read_env_var BACKUP_USER "$SRC_DIR/.env.example")"
fi
SERVICE_USER="${SERVICE_USER:-backup_user}"

if [[ -f "$SRC_DIR/.env" ]]; then
  INTERVAL_HOURS="$(read_env_var BACKUP_INTERVAL_HOURS "$SRC_DIR/.env")"
else
  INTERVAL_HOURS="$(read_env_var BACKUP_INTERVAL_HOURS "$SRC_DIR/.env.example")"
fi
INTERVAL_HOURS="${INTERVAL_HOURS:-2}"

echo ">> Creating service user '$SERVICE_USER' (if needed)..."
if ! id "$SERVICE_USER" &>/dev/null; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo ">> Creating directories..."
mkdir -p "$INSTALL_DIR/scripts" "$INSTALL_DIR/dumps"

# IMPORTANT: create the log file and set permissions BEFORE the service
# ever runs as the service user, otherwise the first run fails with a
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
  sed -i "s|^BACKUP_DIR=.*|BACKUP_DIR=${INSTALL_DIR}/dumps|" "$INSTALL_DIR/.env"
  sed -i "s|^LOG_FILE=.*|LOG_FILE=${LOG_FILE}|" "$INSTALL_DIR/.env"
  sed -i "s|^PROJECT_NAME=.*|PROJECT_NAME=${PROJECT_NAME}|" "$INSTALL_DIR/.env"
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

echo ">> Generating systemd units for '${SERVICE_NAME}'..."
sed -e "s|__PROJECT_NAME__|${PROJECT_NAME}|g" \
    -e "s|__SERVICE_USER__|${SERVICE_USER}|g" \
    -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
    "$SRC_DIR/systemd/service.template" > "/etc/systemd/system/${SERVICE_NAME}.service"

sed -e "s|__PROJECT_NAME__|${PROJECT_NAME}|g" \
    -e "s|__INTERVAL_HOURS__|${INTERVAL_HOURS}|g" \
    "$SRC_DIR/systemd/timer.template" > "/etc/systemd/system/${SERVICE_NAME}.timer"

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.timer"

echo
echo ">> Done."
echo "   Check timer status : systemctl status ${SERVICE_NAME}.timer"
echo "   Check next run time: systemctl list-timers ${SERVICE_NAME}.timer"
echo "   Test run manually  : sudo -u $SERVICE_USER $INSTALL_DIR/scripts/run_backup.sh"
echo "   Watch logs         : tail -f $LOG_FILE"
