#!/usr/bin/env bash
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
#
# uninstall.sh — completely remove an eximaro install (or a legacy 'signage' one).
# Stops and deletes the services + kiosk, the helper CLIs, the app code, the
# service user, and — unless --keep-data — the data dir. Safe to re-run; whatever
# is already gone is skipped. Migrate a device off the old name:
#
#   sudo ./uninstall.sh signage              # purge the old install completely
#   sudo ./uninstall.sh signage --keep-data  # keep /var/lib/signage to migrate it
#   sudo ./uninstall.sh                       # remove eximaro (default)
#
# It only removes what this project created: things named after the app, plus the
# service user. It deliberately leaves the blank cursor theme alone (the installer
# recreates it) and never touches anything else on the system.

set -uo pipefail   # not -e: press on past pieces that are already gone

APP="eximaro"; KEEP_DATA=0; ASSUME_YES=0
for a in "$@"; do
  case "$a" in
    --keep-data) KEEP_DATA=1 ;;
    -y|--yes)    ASSUME_YES=1 ;;
    -*)          echo "unknown option: $a" >&2; exit 2 ;;
    *)           APP="$a" ;;
  esac
done
case "$APP" in ''|*[!a-zA-Z0-9-]*) echo "invalid app name: '$APP'" >&2; exit 2 ;; esac
[ "$(id -u)" -eq 0 ] || { echo "run with sudo (need root to remove services and the user)." >&2; exit 1; }

APP_HOME="/opt/${APP}"; RELEASES="/opt/${APP}-releases"; DATA_DIR="/var/lib/${APP}"
UNITS="${APP}.service ${APP}-kiosk.service ${APP}-promote.path ${APP}-promote.service ${APP}-update.path ${APP}-update.service ${APP}-wifi.path ${APP}-wifi.service"
CLIS="/usr/local/sbin/${APP}-promote /usr/local/sbin/${APP}-update /usr/local/sbin/${APP}-wifi /usr/local/sbin/${APP}-update-full"

echo "This will remove the '${APP}' install:"
echo "  services : ${UNITS}"
echo "  helpers  : ${CLIS}"
echo "  app code : ${APP_HOME}  ${RELEASES}"
echo "  user     : ${APP}"
if [ "$KEEP_DATA" -eq 1 ]; then
  echo "  data     : ${DATA_DIR}   (KEPT)"
else
  echo "  data     : ${DATA_DIR}   (DELETED — pairings, content, and secrets)"
fi
if [ "$ASSUME_YES" -ne 1 ]; then
  read -rp "Continue? [y/N] " ans
  case "$ans" in y|Y|yes|YES) ;; *) echo "aborted."; exit 0 ;; esac
fi

echo "==> stopping + disabling services"
for u in $UNITS; do systemctl disable --now "$u" >/dev/null 2>&1 || true; done

echo "==> deleting unit files"
for u in $UNITS; do rm -f "/etc/systemd/system/${u}"; done
systemctl daemon-reload
systemctl reset-failed >/dev/null 2>&1 || true

echo "==> deleting helper CLIs"
# shellcheck disable=SC2086
rm -f $CLIS

echo "==> deleting app code (${APP_HOME}, ${RELEASES})"
rm -rf "$APP_HOME" "$RELEASES"

if [ "$KEEP_DATA" -eq 1 ]; then
  echo "==> keeping ${DATA_DIR}"
else
  echo "==> deleting ${DATA_DIR}"
  rm -rf "$DATA_DIR"
fi

echo "==> removing service user '${APP}'"
loginctl disable-linger "$APP" >/dev/null 2>&1 || true
if id "$APP" >/dev/null 2>&1; then
  pkill -u "$APP" >/dev/null 2>&1 || true
  sleep 1
  userdel -r "$APP" >/dev/null 2>&1 || userdel "$APP" >/dev/null 2>&1 || true
fi

echo "==> restoring console login on tty1"
systemctl enable --now getty@tty1.service >/dev/null 2>&1 || true

echo
echo "Done — '${APP}' is removed."
echo "Verify:  systemctl list-units --all | grep -i ${APP}   (expect nothing)"
echo "         ss -ltnp | grep ':8080'                        (expect nothing)"
[ "$KEEP_DATA" -eq 1 ] && echo "Kept ${DATA_DIR} — copy it to the new install's data dir to migrate."
