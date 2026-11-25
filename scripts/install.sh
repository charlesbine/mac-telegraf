#!/usr/bin/env bash
# Install mac-telegraf into ~/.local/share/mac-telegraf (outside TCC-protected
# folders like ~/Documents or ~/Desktop) and register the macOS LaunchAgent.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTALL_DIR="${MAC_TELEGRAF_INSTALL_DIR:-${HOME}/.local/share/mac-telegraf}"
PLIST_PATH="${HOME}/Library/LaunchAgents/com.charlesbine.mac-telegraf.plist"
LABEL="com.charlesbine.mac-telegraf"

mkdir -p "${INSTALL_DIR}/scripts" "${HOME}/Library/LaunchAgents"

cp "${REPO_DIR}/scripts/mac_telemetry.py" "${INSTALL_DIR}/scripts/mac_telemetry.py"
cp "${REPO_DIR}/scripts/powerlog_backfill.py" "${INSTALL_DIR}/scripts/powerlog_backfill.py"
cp "${REPO_DIR}/scripts/run_telegraf.sh" "${INSTALL_DIR}/scripts/run_telegraf.sh"
chmod +x "${INSTALL_DIR}/scripts/mac_telemetry.py" \
         "${INSTALL_DIR}/scripts/powerlog_backfill.py" \
         "${INSTALL_DIR}/scripts/run_telegraf.sh"

if [[ -f "${REPO_DIR}/telegraf.conf" ]]; then
  cp "${REPO_DIR}/telegraf.conf" "${INSTALL_DIR}/telegraf.conf"
elif [[ ! -f "${INSTALL_DIR}/telegraf.conf" ]]; then
  cp "${REPO_DIR}/telegraf.conf.example" "${INSTALL_DIR}/telegraf.conf"
fi

if [[ -f "${REPO_DIR}/.env" ]]; then
  umask 077
  cp "${REPO_DIR}/.env" "${INSTALL_DIR}/.env"
  chmod 600 "${INSTALL_DIR}/.env"
elif [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  echo "Missing .env. Copy .env.example to .env (or ${INSTALL_DIR}/.env) first." >&2
  exit 1
fi

UID_NUM="$(id -u)"
if launchctl print "gui/${UID_NUM}" >/dev/null 2>&1; then
  DOMAIN="gui/${UID_NUM}"
  SESSION_TYPE="Aqua"
else
  DOMAIN="user/${UID_NUM}"
  SESSION_TYPE="Background"
fi

sed -e "s|/PATH/TO/mac-telegraf|${INSTALL_DIR}|g" \
    -e "s|__SESSION_TYPE__|${SESSION_TYPE}|g" \
    "${REPO_DIR}/launchd/com.charlesbine.mac-telegraf.plist.example" > "${PLIST_PATH}"

if launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
  launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
  sleep 1
fi
launchctl bootstrap "${DOMAIN}" "${PLIST_PATH}"

echo "Installed to ${INSTALL_DIR} and loaded ${LABEL} in ${DOMAIN} (${SESSION_TYPE})."
