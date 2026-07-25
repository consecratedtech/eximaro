#!/usr/bin/env bash
# update.sh — pull the latest eximaro from GitHub and (re)install it, the whole way.
#
# The in-app "Update now" only swaps the app code. This does a FULL update: it grabs
# the newest code from GitHub and runs install.sh, so it also refreshes the pieces
# "Update now" can't touch — the WiFi helper, the systemd units, and a kiosk reload.
# It keeps this device's role and everything under /var/lib/eximaro — the
# display<->controller pairings and all your content/URLs — which it never touches.
#
# Usage:
#   sudo bash update.sh            # figures out display vs controller on its own
#   sudo bash update.sh display    # force the role if it can't be read
set -euo pipefail

APP="eximaro"
DATA_DIR="/var/lib/${APP}"
REPO_URL="${REPO_URL:-https://github.com/consecratedtech/eximaro.git}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo:  sudo bash update.sh"
  exit 1
fi

# Use this device's current role, so there's nothing to pick and nothing to get wrong.
ROLE="${1:-}"
if [ -z "$ROLE" ] && [ -f "${DATA_DIR}/config.json" ]; then
  ROLE="$(python3 -c "import json; print(json.load(open('${DATA_DIR}/config.json')).get('role') or '')" 2>/dev/null || true)"
fi

echo "==> Downloading the latest eximaro from GitHub..."
if ! command -v git >/dev/null 2>&1; then
  echo "git isn't installed, so this can't self-update. Re-run install.sh from a clone."
  exit 1
fi
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if ! git clone --depth 1 "$REPO_URL" "$TMP/eximaro"; then
  echo "Couldn't download the update. Either this device is offline, or the"
  echo "repository is private and this device has no access to it yet."
  exit 1
fi

echo "==> Installing the latest (role: ${ROLE:-will ask})..."
cd "$TMP/eximaro"
if [ "$ROLE" = "display" ] || [ "$ROLE" = "controller" ]; then
  bash install.sh --role "$ROLE"
else
  bash install.sh   # role unknown (fresh device) -> install.sh will ask
fi
