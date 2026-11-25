#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

export MAC_TELEGRAF_DIR="${REPO_DIR}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH}"

ENV_FILE="${MAC_TELEGRAF_ENV:-${REPO_DIR}/.env}"
CONF_FILE="${MAC_TELEGRAF_CONF:-${REPO_DIR}/telegraf.conf}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}. Copy .env.example to .env and fill in your InfluxDB settings." >&2
  exit 1
fi

if [[ ! -f "${CONF_FILE}" ]]; then
  echo "Missing ${CONF_FILE}. Copy telegraf.conf.example to telegraf.conf." >&2
  exit 1
fi

set -a
# shellcheck source=/dev/null
source "${ENV_FILE}"
set +a

: "${MAC_HOSTNAME:=$(scutil --get LocalHostName 2>/dev/null || hostname -s)}"
: "${MAC_HOST_ROLE:=laptop}"
export MAC_HOSTNAME MAC_HOST_ROLE

TELEGRAF_BIN="$(command -v telegraf || true)"
if [[ -z "${TELEGRAF_BIN}" ]]; then
  echo "telegraf not found in PATH. Install with: brew install telegraf" >&2
  exit 1
fi

exec "${TELEGRAF_BIN}" --config "${CONF_FILE}"
