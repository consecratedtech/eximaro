#!/usr/bin/env bash
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
#
# install.sh — bootstrap installer for the eximaro appliance (v1)
# Target: Debian 13 (trixie) on x86_64, and Raspberry Pi OS Lite (trixie) on Pi 4/5.
#
# Usage:
#   git clone <repo> && cd <repo> && sudo ./install.sh
#   ...or pipe it:  curl -sSL <raw-url>/install.sh | sudo bash
#
# Optional flags:
#   --role controller|display   Skip the interactive role prompt.
#   --force                     Continue even if the OS/arch check fails.
#   --check                     Run diagnostics only, install nothing.
#
set -euo pipefail

# ---- settings ---------------------------------------------------------------
APP="eximaro"                       # the product name — drives paths, users, and unit names
APP_USER="eximaro"
APP_HOME="/opt/${APP}"              # a SYMLINK to the active release (A/B updates)
RELEASES="/opt/${APP}-releases"     # versioned release dirs; APP_HOME points at one
DATA_DIR="/var/lib/${APP}"          # disk-backed; holds secrets + cached content
WORK_DIR="${DATA_DIR}/work"         # conversion scratch (NOT /tmp — tmpfs on trixie)
WEB_PORT="8080"
# Where to fetch the code when this script is run on its own (the curl | bash
# one-liner) instead of from a checked-out repo. Override via env for a fork.
REPO_URL="${REPO_URL:-https://github.com/consecratedtech/eximaro.git}"
ROLE=""
FORCE=0
CHECK_ONLY=0

# ---- pretty output ----------------------------------------------------------
c() { printf '\033[%sm%s\033[0m' "$1" "$2"; }
ok()   { echo "$(c '0;32' '  ok ') $*"; }
warn() { echo "$(c '0;33' 'warn ') $*"; }
die()  { echo "$(c '0;31' 'FAIL ') $*" >&2; exit 1; }
step() { echo; echo "$(c '1;36' "==> $*")"; }

# Install a helper script only if the source is non-empty, and keep any working copy
# already on the device otherwise. An empty script is still executable: it runs, exits
# 0, and does nothing — which is how a truncated download once replaced the updater
# with a no-op and left the device with no way to update itself.
install_script() {
  if [ ! -s "$1" ]; then
    warn "refusing to install an empty $(basename "$2") — keeping the existing one"
    return 0
  fi
  # Write beside the target, then RENAME over it. Never write in place: one of these
  # targets is ${APP}-update-full, i.e. the very script running this update. install(1)
  # truncates the destination first, and bash reads a script lazily from that same
  # inode — so overwriting it mid-run corrupts the running update, and an interruption
  # between truncate and write leaves a 0-byte file: an "updater" that runs, exits 0,
  # and does nothing. rename() swaps in a new inode atomically, so the running shell
  # keeps reading the old one safely and the target is never half-written.
  _is_tmp="$2.new.$$"
  cp "$1" "$_is_tmp" && chmod 0755 "$_is_tmp" && mv -f "$_is_tmp" "$2" || {
    rm -f "$_is_tmp"
    warn "could not update $(basename "$2") — keeping the existing one"
    return 0
  }
}

# Record the update outcome where the RUNNING app can see it, so a failed/rolled-back
# update is never silent — the panel shows a banner and the screen shows a notice.
# Same file + format the in-app updater writes. detail is sanitised so an error
# message can't produce broken JSON.
update_status() {
  _us_detail="$(printf '%s' "${2:-}" | tr -d '\r\n' | tr '"\\' "' " | cut -c1-200)"
  # Never create or re-mode the data dir here: it is 0700 and holds the secrets
  # (master key, vault). Creating it 0755 would silently widen those permissions on
  # every update. If it doesn't exist yet there is no app to show the status anyway.
  [ -d "$DATA_DIR" ] || return 0
  rm -f "$DATA_DIR/update.status" 2>/dev/null || true
  printf '{"state":"%s","detail":"%s","when":"%s"}\n' "$1" "$_us_detail" "$(date -Is)" \
    > "$DATA_DIR/update.status" 2>/dev/null || true
  chown -h "$APP_USER":"$APP_USER" "$DATA_DIR/update.status" 2>/dev/null || true
}

# ---- args -------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --role) ROLE="${2:-}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --check) CHECK_ONLY=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done

# ---- pre-flight checks ------------------------------------------------------
step "Pre-flight checks"

[ "$(id -u)" -eq 0 ] || die "run with sudo (need root to install packages)."

# OS: must be Debian 13 trixie (Raspberry Pi OS trixie reports ID=raspbian/debian, VERSION_CODENAME=trixie)
. /etc/os-release 2>/dev/null || die "cannot read /etc/os-release"
if [ "${VERSION_CODENAME:-}" = "trixie" ] || [ "${VERSION_ID:-}" = "13" ]; then
  ok "OS is Debian 13 / trixie (${PRETTY_NAME:-unknown})"
else
  warn "expected Debian 13 (trixie); found '${PRETTY_NAME:-unknown}'"
  [ "$FORCE" -eq 1 ] || die "unsupported OS. Re-run with --force to override."
fi

# Arch: arm64 (Pi 4/5) or amd64 (x86 PC)
ARCH="$(dpkg --print-architecture)"
case "$ARCH" in
  arm64|amd64) ok "architecture: ${ARCH}" ;;
  *) warn "untested architecture: ${ARCH}";
     [ "$FORCE" -eq 1 ] || die "unsupported arch. Re-run with --force to override." ;;
esac

# Disk space (need ~2 GB headroom; LibreOffice alone is large)
FREE_MB="$(df -Pm / | awk 'NR==2{print $4}')"
if [ "${FREE_MB:-0}" -lt 2048 ]; then
  warn "low free space on / : ${FREE_MB} MB (recommend >= 2048 MB)"
else
  ok "free space: ${FREE_MB} MB"
fi

# Network reachable for apt
if ping -c1 -W2 deb.debian.org >/dev/null 2>&1; then
  ok "network reachable"
else
  warn "could not reach deb.debian.org — apt may fail"
fi

# Which chromium package exists on this OS?
CHROMIUM_PKG=""
for p in chromium chromium-browser; do
  if apt-cache show "$p" >/dev/null 2>&1; then CHROMIUM_PKG="$p"; break; fi
done
[ -n "$CHROMIUM_PKG" ] && ok "chromium package: ${CHROMIUM_PKG}" || warn "no chromium package found in apt"

if [ "$CHECK_ONLY" -eq 1 ]; then
  step "Check-only mode: stopping before any changes."
  exit 0
fi

# ---- role -------------------------------------------------------------------
step "Role"
if [ -z "$ROLE" ]; then
  echo "Every device runs the same app. Pick this device's starting role:"
  echo "  1) display     — shows content (lightweight; runs on small devices)"
  echo "  2) controller  — also displays, plus runs the control panel + conversion"
  read -rp "Enter 1 or 2: " choice
  case "$choice" in
    1) ROLE="display" ;;
    2) ROLE="controller" ;;
    *) die "invalid choice." ;;
  esac
fi
case "$ROLE" in
  display|controller) ok "role: ${ROLE}" ;;
  *) die "role must be 'display' or 'controller'." ;;
esac
echo "(role is switchable later in settings; switching to controller will fetch the extra packages then.)"

# ---- packages ---------------------------------------------------------------
step "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

# common to both roles
COMMON_PKGS="python3 python3-venv python3-dev python3-pip \
  git curl ca-certificates \
  avahi-daemon avahi-utils \
  nftables network-manager \
  cage ${CHROMIUM_PKG} \
  pipewire pipewire-pulse wireplumber libspa-0.2-modules \
  fonts-dejavu"

# controller also renders/converts, so it needs LibreOffice + PDF->image tools
CONTROLLER_PKGS="libreoffice-impress libreoffice-core poppler-utils"

PKGS="$COMMON_PKGS"
[ "$ROLE" = "controller" ] && PKGS="$PKGS $CONTROLLER_PKGS"

# shellcheck disable=SC2086
apt-get install -y --no-install-recommends $PKGS
ok "system packages installed"

# ---- app user + dirs --------------------------------------------------------
step "Creating app user and directories"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/home/${APP_USER}" \
          --shell /usr/sbin/nologin "$APP_USER"
  ok "created user ${APP_USER}"
else
  ok "user ${APP_USER} already exists"
fi
# render group lets cage/chromium reach the GPU/DRM device on display nodes;
# audio lets PipeWire reach the sound card (HDMI) from the user's session
usermod -aG video,render,input,audio "$APP_USER" 2>/dev/null || true
# linger gives this (system) user a persistent /run/user/<uid>; cage needs it for
# XDG_RUNTIME_DIR even though the kiosk runs from systemd, not an interactive login.
loginctl enable-linger "$APP_USER" 2>/dev/null || true

install -d -m 0755 "$RELEASES"      # APP_HOME itself becomes a symlink (created below)
install -d -m 0700 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR"
install -d -m 0700 -o "$APP_USER" -g "$APP_USER" "$WORK_DIR"
ok "data dir ${DATA_DIR} (0700 — secrets stay here, never world-readable)"

# ---- audio: route sound to HDMI --------------------------------------------
# Videos can play with sound (the "Play sound" toggle). PipeWire (installed above,
# shipping user services that auto-start in the lingering ${APP_USER} session)
# gives Chromium a sound server; it finds the socket via XDG_RUNTIME_DIR. By
# default PipeWire may pick the analog jack, so a per-session oneshot points the
# default sink at whatever sink reports "HDMI" — a Pi's vc4hdmi OR a PC's
# Intel/AMD HDMI — so the same setup works on both arches without hardcoding.
step "Setting up audio (PipeWire -> HDMI)"
APP_UID="$(id -u "$APP_USER")"
AUDIO_CONF="/etc/${APP}/audio-sink"   # operator override: pin a specific output
cat > /usr/local/bin/${APP}-audio-hdmi <<HDMI
#!/bin/sh
# Point the default sink at the display's audio output. Runs in the app user's
# PipeWire session at login; matches the sink by NAME so it is not tied to a card
# index. A Pi says "(HDMI)"; an x86 GPU says "HDMI n" or, for a DisplayPort
# monitor, "DisplayPort" — so match both, and the same setup works on either arch.
#
# When that guess is wrong — a USB-C dock presenting itself as a generic
# "USB Audio" device, an amplifier, a second HDMI port — an operator can PIN the
# output by writing a sink id or a name fragment into:
#
#     ${AUDIO_CONF}
#
# Run "${APP}-audio-hdmi --list" to see the choices, then e.g.
#     echo 'USB Audio' | sudo tee ${AUDIO_CONF} && sudo systemctl reboot
#
# Usage: ${APP}-audio-hdmi [--list | --status]
AUDIO_CONF="${AUDIO_CONF}"
APP_USER="${APP_USER}"
APP_UID="${APP_UID}"
HDMI
cat >> /usr/local/bin/${APP}-audio-hdmi <<'HDMI'

# Sound lives in the app user's PipeWire session, so wpctl only sees it from there.
# Re-run as that user when an operator invokes this by hand (as root, or via sudo);
# the systemd user service already runs as the app user and skips this.
if [ "$(id -u)" != "$APP_UID" ]; then
  exec sudo -u "$APP_USER" \
    env XDG_RUNTIME_DIR="/run/user/$APP_UID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$APP_UID/bus" "$0" "$@"
fi

# Every AUDIO sink as "<id> <name>". wpctl prints a "Sinks:" block under BOTH the
# Audio and the Video section, so this is scoped to Audio — otherwise a camera or
# other video node can be selected as the sound output. Box-drawing characters are
# stripped so the ids parse on any locale.
# LC_ALL=C on the text steps is REQUIRED, not tidiness: wpctl draws the tree with
# box characters, and under a UTF-8 locale GNU sed reads [^ -~] as a collation range
# rather than the ASCII bytes — it then eats the digits and letters too, leaving no
# sinks at all (silently: every device would fall back to "no output found").
list_sinks() {
  wpctl status 2>/dev/null | LC_ALL=C awk '
    /^Audio$/ { a = 1; next }
    /^Video$/ { a = 0 }
    a && /Sinks:/ { s = 1; next }
    a && /(Sources|Sink endpoints|Filters|Streams):/ { s = 0 }
    a && s { print }
  ' | LC_ALL=C sed 's/[^ -~]//g' \
    | LC_ALL=C sed -n 's/^[^0-9]*\([0-9][0-9]*\)\.[[:space:]]*\(.*\)$/\1 \2/p' \
    | LC_ALL=C sed 's/[[:space:]]*\[vol:.*$//'
}

# Resolve a pin to a sink id, echoing nothing when it matches no CURRENT sink.
# A pin is matched, in order: an exact id, then an exact name, then a
# case-insensitive part of a name. Names are matched LITERALLY (grep -F) — a real
# output is called things like "Built-in Audio Digital Stereo (HDMI)", and those
# parentheses are regex syntax, so a regex match would fail to find the very device
# the panel just listed.
resolve() {
  [ -n "$1" ] || return 0
  case "$1" in
    ''|*[!0-9]*) ;;                                          # not an id — fall through
    *) list_sinks | awk -v want="$1" '$1 == want { print $1; exit }' | grep . && return 0 ;;
  esac
  # Exact name first, so a short name can never be swallowed by a longer one that
  # merely contains it; then the looser hand-written fragment.
  list_sinks | awk -v want="$1" '{ name = substr($0, index($0, " ") + 1) }
                                 name == want { print $1; exit }' | grep . && return 0
  list_sinks | LC_ALL=C grep -iF -- "$1" | awk '{ print $1; exit }'
}

# A pin wins over the HDMI guess. The file ships full of comments explaining itself,
# so read the first line that is neither a comment nor blank — an untouched file
# leaves this empty, which means "keep choosing automatically".
PIN=""
[ -r "$AUDIO_CONF" ] && PIN=$(LC_ALL=C sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$AUDIO_CONF" 2>/dev/null | head -1 \
                              | LC_ALL=C sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

# The id of the sink PipeWire is using now (wpctl marks it with a "*").
current_sink() {
  wpctl status 2>/dev/null | LC_ALL=C awk '
    /^Audio$/ { a = 1; next }
    /^Video$/ { a = 0 }
    a && /Sinks:/ { s = 1; next }
    a && /(Sources|Sink endpoints|Filters|Streams):/ { s = 0 }
    a && s { print }
  ' | LC_ALL=C sed 's/[^ -~]//g' | LC_ALL=C grep '\*' \
    | LC_ALL=C sed -n 's/^[^0-9]*\([0-9][0-9]*\)\..*$/\1/p' | head -1
}

case "$1" in
  --list)
    echo "Audio outputs this device can use:"; list_sinks | sed 's/^/  /'
    [ -n "$PIN" ] && echo "Pinned to: $PIN   (in $AUDIO_CONF)"
    [ -z "$PIN" ] && echo "No pin set — choosing HDMI/DisplayPort automatically."
    exit 0 ;;
  --raw)     list_sinks; exit 0 ;;          # "<id> <name>" per line, for the panel
  --current) current_sink; exit 0 ;;        # id of the sink in use right now
  --pin)     printf '%s\n' "$PIN"; exit 0 ;;
  --status)
    wpctl status 2>/dev/null | LC_ALL=C awk '/^Audio$/{a=1} /^Video$/{a=0} a' | head -20; exit 0 ;;
esac

# The display's sink can appear a little after the session starts (and not at all
# while the TV is off), so poll briefly rather than give up on the first look.
ID=""
for _ in $(seq 1 30); do
  if [ -n "$PIN" ]; then
    ID=$(resolve "$PIN")
  else
    ID=$(list_sinks | grep -iE 'hdmi|displayport' | awk '{ print $1; exit }')
  fi
  [ -n "$ID" ] && break
  sleep 1
done

if [ -z "$ID" ]; then
  # Leave whatever PipeWire chose — silence is better than a hard failure, and the
  # reason is in the journal for anyone troubleshooting.
  if [ -n "$PIN" ]; then
    echo "audio: nothing matches the pinned output '$PIN'; leaving the default. Choices:"
    list_sinks | sed 's/^/  /'
  else
    echo "audio: no HDMI/DisplayPort output found (is the screen on?); leaving the default."
  fi
  exit 0
fi

wpctl set-default "$ID"
wpctl set-volume "$ID" 1.0        # full at the sink; the TV's own volume still applies
echo "audio: default output -> $(list_sinks | awk -v i="$ID" '$1 == i { $1 = ""; print }')"
HDMI
chmod 0755 /usr/local/bin/${APP}-audio-hdmi

# Seed a commented placeholder so the override is discoverable on the device itself.
# Re-seed when the explanation is missing too (an older device, or a file that got
# emptied), carrying any pin already set across — so every device converges on a
# self-explaining file without an operator's choice ever being lost.
install -d -m 0755 "$(dirname "${AUDIO_CONF}")"
if [ ! -f "${AUDIO_CONF}" ] || ! grep -q '^# Which audio output' "${AUDIO_CONF}" 2>/dev/null; then
  _keep=""
  if [ -f "${AUDIO_CONF}" ]; then
    _keep="$(grep -vE '^[[:space:]]*(#|$)' "${AUDIO_CONF}" 2>/dev/null | head -1 || true)"
  fi
  cat > "${AUDIO_CONF}" <<CONF
# Which audio output ${APP} should use, when the automatic "HDMI/DisplayPort"
# choice is wrong (a USB-C dock, an amplifier, a second HDMI port).
#
# Put ONE line below: either a sink id, or part of its name (case-insensitive).
# See the choices with:   ${APP}-audio-hdmi --list
# Example:                USB Audio
#
# Leave this file with no active line to keep the automatic choice.
CONF
  [ -n "$_keep" ] && printf '%s\n' "$_keep" >> "${AUDIO_CONF}"   # keep an existing pin
fi
cat > /etc/systemd/user/${APP}-audio-hdmi.service <<UNIT
[Unit]
Description=Route ${APP} audio to the HDMI output
After=wireplumber.service
Wants=wireplumber.service
[Service]
Type=oneshot
RemainAfterExit=yes
# Tag the journal so an operator can read just this: journalctl -t ${APP}-audio-hdmi
SyslogIdentifier=${APP}-audio-hdmi
ExecStart=/usr/local/bin/${APP}-audio-hdmi
[Install]
WantedBy=default.target
UNIT
# Enable it for the app user by hand (a symlink), so it does not depend on the
# user session being up mid-install; PipeWire's own units are package-enabled.
install -d -m 0755 -o "$APP_USER" -g "$APP_USER" "/home/${APP_USER}/.config/systemd/user/default.target.wants"
ln -sf /etc/systemd/user/${APP}-audio-hdmi.service \
  "/home/${APP_USER}/.config/systemd/user/default.target.wants/${APP}-audio-hdmi.service"
chown -h "$APP_USER":"$APP_USER" "/home/${APP_USER}/.config/systemd/user/default.target.wants/${APP}-audio-hdmi.service"
# If the session is already up (a re-install), apply it now too; harmless if not.
_uenv="XDG_RUNTIME_DIR=/run/user/${APP_UID} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${APP_UID}/bus"
# shellcheck disable=SC2086
sudo -u "$APP_USER" env $_uenv systemctl --user daemon-reload 2>/dev/null || true
# shellcheck disable=SC2086
sudo -u "$APP_USER" env $_uenv systemctl --user start ${APP}-audio-hdmi.service 2>/dev/null || true
ok "audio routed to HDMI (PipeWire); videos with 'Play sound' will use it"

# ---- hide the mouse cursor --------------------------------------------------
# An eximaro screen has no operator, so the pointer must never show. Two facts,
# both established on real hardware, decide how this has to be done:
#
#   1. There IS a pointer, even with nothing plugged in. A TV's HDMI-CEC channel
#      registers as an input device that advertises REL_X/REL_Y (look for
#      "vc4-hdmi-0" with REL=3 in /proc/bus/input/devices), so libinput classifies
#      the television itself as a mouse and the compositor parks a pointer in the
#      middle of the screen, where it sits forever because it never moves.
#   2. Chromium picks its OWN cursor theme and ignores XCURSOR_THEME. Tracing it
#      shows it reading /usr/share/icons/Adwaita/cursors — so a "blank" theme
#      installed alongside the real ones does nothing, and neither does
#      `cursor:none` in the page (Chromium only re-evaluates the CSS cursor on a
#      pointer event, and this pointer never sends one).
#
# So instead of arguing with Chromium about which theme to use, point the cursor
# SEARCH PATH at a directory we own in which every theme name it might ask for
# resolves to fully-transparent images. Nothing under /usr/share/icons is touched,
# an apt upgrade can't undo it, and whatever theme name is requested — including
# one we never thought of, which falls back to "default" — comes out invisible.
#
# Things that do NOT work, so nobody re-tries them: a udev LIBINPUT_IGNORE_DEVICE
# rule on the CEC device (the compositor then stops serving frames — the screen
# capture blocks forever), and 1x1 transparent images (they parse fine but are not
# what gets drawn; use real-size ones).
step "Hiding the mouse cursor"
CURSOR_DIR="/usr/share/${APP}/cursors"
rm -rf "$CURSOR_DIR"
install -d -m 0755 "${CURSOR_DIR}/_blank/cursors"
python3 - "$CURSOR_DIR" <<'PY'
import struct, sys
# One fully-transparent image per nominal size an xcursor loader may ask for.
sizes = [16, 24, 32, 48, 64]
chunks = [struct.pack('<IIIIIIIII', 36, 0xfffd0002, s, 1, s, s, 0, 0, 0) + b'\0' * (s * s * 4)
          for s in sizes]
hdr = b'Xcur' + struct.pack('<III', 16, 0x00010000, len(chunks))
pos = 16 + len(chunks) * 12
offs = []
for c in chunks:
    offs.append(pos); pos += len(c)
toc = b''.join(struct.pack('<III', 0xfffd0002, s, o) for s, o in zip(sizes, offs))
open(sys.argv[1] + '/_blank/cursors/left_ptr', 'wb').write(hdr + toc + b''.join(chunks))
PY
# Every cursor name a browser, toolkit or compositor might request -> the same
# invisible image. A name that is missing falls through to a real theme, which is
# exactly how the pointer came back, so this list is deliberately exhaustive.
( cd "${CURSOR_DIR}/_blank/cursors"
  for n in default pointer arrow top_left_arrow left_ptr_watch xterm text ibeam hand hand1 hand2 \
           pointing_hand grab grabbing openhand closedhand watch wait progress crosshair cross \
           fleur move all-scroll col-resize row-resize size_all size_ver size_hor n-resize \
           e-resize s-resize w-resize ne-resize nw-resize se-resize sw-resize ew-resize \
           ns-resize nesw-resize nwse-resize not-allowed no-drop forbidden dnd-none dnd-copy \
           dnd-move dnd-link help question_arrow context-menu copy alias zoom-in zoom-out cell \
           vertical-text right_ptr center_ptr sb_h_double_arrow sb_v_double_arrow \
           bottom_left_corner bottom_right_corner top_left_corner top_right_corner \
           left_side right_side top_side bottom_side X_cursor; do
    ln -sf left_ptr "$n"
  done )
# The theme names Chromium/GTK actually reach for. None of them inherit anything,
# so the chain can never walk out of this directory into a real cursor set.
for _t in default Adwaita AdwaitaLegacy hicolor; do
  install -d -m 0755 "${CURSOR_DIR}/${_t}"
  ln -sfn "${CURSOR_DIR}/_blank/cursors" "${CURSOR_DIR}/${_t}/cursors"
  printf '[Icon Theme]\nName=%s\nComment=eximaro: a sign has no pointer\n' "$_t" \
    > "${CURSOR_DIR}/${_t}/index.theme"
done
# Undo the older attempt (a "blank" theme dropped into the system icon dirs). It
# never worked, and leaving a symlink inside a packaged directory behind is rude.
if [ -L /usr/share/icons/default/cursors ] \
   && [ "$(readlink /usr/share/icons/default/cursors)" = "/usr/share/icons/blank/cursors" ]; then
  rm -f /usr/share/icons/default/cursors
fi
rm -rf /usr/share/icons/blank
ok "pointer hidden (every cursor theme resolves to a transparent image)"

# ---- app code (A/B release) -------------------------------------------------
# Each version installs into its own release dir under ${RELEASES}; ${APP_HOME} is
# a symlink to the active one. Activating is a single atomic symlink swap, so a
# power cut mid-update can never leave a half-written app — worst case is "still on
# the previous release." The updater (installed below) reuses this same layout.
step "Placing app code (A/B release)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SRC_DIR}/requirements.txt" ]; then
  ok "running from a checked-out repo"
elif [ -n "$REPO_URL" ]; then
  ok "cloning ${REPO_URL}"
  rm -rf "${WORK_DIR}/_src"
  git clone --depth 1 "$REPO_URL" "${WORK_DIR}/_src"
  SRC_DIR="${WORK_DIR}/_src"
else
  die "no app source found. Run from the cloned repo, or set REPO_URL when piping."
fi

# Refuse to install from an incomplete source tree. A download interrupted by a weak
# network can leave files EMPTY rather than missing, and an empty file installs
# perfectly happily — that is how a device once ended up with a 0-byte
# ${APP}-update-full: a script that "runs" fine and silently does nothing, leaving the
# operator with no way to update. Check the pieces we copy out verbatim are present
# AND non-empty, before anything on the device is touched.
for _f in requirements.txt update.sh app/server.py app/__main__.py \
          helpers/eximaro-wifi helpers/eximaro-set-url app/pages/screen.html; do
  [ -s "${SRC_DIR}/${_f}" ] || die "the downloaded copy is incomplete (${_f} is missing or empty).
       Nothing on this device was changed. This usually means a weak connection during
       the download — try again on a stronger network."
done
ok "source tree looks complete"

REL="${RELEASES}/rel-$(date +%s)"
rm -rf "$REL"; mkdir -p "$REL"
cp -a "${SRC_DIR}/." "$REL/"
# Never ship a copied-in dev virtualenv or repo metadata: a stale .venv carries
# absolute-path shebangs from the source checkout (breaks pip), and .git is dead
# weight on the appliance. The venv below is always built fresh.
rm -rf "$REL/.venv" "$REL/.git"
chown -R "$APP_USER":"$APP_USER" "$REL"
ok "release staged at ${REL}"

step "Setting up Python environment (venv — required on trixie/PEP 668)"
sudo -u "$APP_USER" python3 -m venv --clear "$REL/.venv"
# --no-cache-dir throughout: a download interrupted by a weak network can leave a
# TRUNCATED wheel in pip's cache, and pip will happily reuse it on every later build —
# installing a package that looks present but is incomplete (import fails at runtime).
# That cost a device its app twice in one evening; rebuilding with --no-cache-dir was
# what fixed it. Re-downloading a few MB is a trivial price for a build that is right.
sudo -u "$APP_USER" "$REL/.venv/bin/pip" install --quiet --no-cache-dir --upgrade pip
if [ -f "$REL/requirements.txt" ]; then
  if ! pip_err="$(sudo -u "$APP_USER" "$REL/.venv/bin/pip" install --no-cache-dir \
                    -r "$REL/requirements.txt" 2>&1)"; then
    printf '%s\n' "$pip_err" | tail -6 | sed 's/^/    /'
    update_status failed "The last update could not download its components (weak connection) and was not applied — this device is still on its previous, working version."
    rm -rf "$REL"
    die "could not install the Python dependencies; nothing on this device was changed."
  fi
  # Trust nothing: confirm every pinned package is actually IMPORTABLE, not merely
  # present on disk. pip has exited 0 with an incomplete install before.
  if ! dep_err="$(sudo -u "$APP_USER" "$REL/.venv/bin/python" - <<'PY' 2>&1
import importlib, sys
missing = []
for mod in ("fastapi", "starlette", "uvicorn", "pydantic", "jinja2",
            "multipart", "argon2", "cryptography", "zeroconf", "PIL", "qrcode"):
    try:
        importlib.import_module(mod)
    except Exception as exc:            # noqa: BLE001 - report anything that fails
        missing.append(f"{mod}: {exc}")
if missing:
    print("\n".join(missing)); sys.exit(1)
PY
  )"; then
    printf '%s\n' "$dep_err" | tail -6 | sed 's/^/    /'
    update_status failed "The last update installed damaged components and was not applied — this device is still on its previous, working version. Try again on a stronger connection."
    rm -rf "$REL"
    die "the installed dependencies are incomplete; nothing on this device was changed."
  fi
  ok "python dependencies installed and verified"
else
  warn "no requirements.txt — skipping pip install"
fi

# Smoke-test the new build BEFORE activating it. Importing app.server pulls in every
# dependency (FastAPI, Starlette, pydantic, ...) without starting a server or binding a
# port, so a broken/mismatched install fails HERE — while the currently-running release
# is still untouched — instead of after activation as a crash loop. This is exactly the
# failure that once stranded a device: a flaky-network update resolved a bad
# FastAPI/Starlette pair and 'import app.server' would have caught it.
if [ -f "$REL/requirements.txt" ]; then
  # Exercise what actually RUNS, not just one module: uvicorn (imported by app.__main__,
  # not by app.server), plus building the real app object — that registers every route,
  # mounts the static files and constructs FastAPI, so a broken resolve anywhere in the
  # web stack fails HERE rather than after activation. Only the server socket is left
  # unbound, which the /healthz gate below covers.
  if ! import_err="$(cd "$REL" && sudo -u "$APP_USER" "$REL/.venv/bin/python" -c '
import app.__main__, uvicorn
from app.server import create_app
create_app()
' 2>&1)"; then
    warn "new build failed its import check — keeping the current version, discarding this one:"
    printf '%s\n' "$import_err" | tail -4 | sed 's/^/    /'
    update_status failed "The last update could not start (a dependency problem) and was not applied — this device is still on its previous, working version."
    rm -rf "$REL"
    die "update aborted before activation; the device is still on its working version."
  fi
  ok "new build imports cleanly"
fi

# Activate this release, remembering what was active so an unhealthy new release can be
# rolled back after restart. An older FLAT install (a real dir, not a symlink) is
# retired into ${RELEASES} first so it remains as a rollback target.
if [ -L "$APP_HOME" ]; then
  PREV_REL="$(readlink -f "$APP_HOME" 2>/dev/null || true)"
elif [ -e "$APP_HOME" ]; then
  rm -rf "${RELEASES}/rel-preexisting"
  if mv "$APP_HOME" "${RELEASES}/rel-preexisting" 2>/dev/null; then PREV_REL="${RELEASES}/rel-preexisting"; else rm -rf "$APP_HOME"; PREV_REL=""; fi
else
  PREV_REL=""
fi
[ "$PREV_REL" = "$REL" ] && PREV_REL=""     # never "roll back" to the release we're installing
ln -sfn "$REL" "$APP_HOME"          # atomic activate
chown -h "$APP_USER":"$APP_USER" "$APP_HOME" 2>/dev/null || true
ok "activated ${APP_HOME} -> ${REL}"
# Pruning old releases is deliberately DEFERRED until the health gate at the end has
# proven this release serves. Pruning here would run before we know the new build
# works, and picks survivors by mtime — so on a device whose clock jumped (no RTC on a
# Pi; fake-hwclock + a late NTP sync), the rollback target could rank old and be
# deleted, leaving nothing to roll back to. See the prune after the health check.

# ---- systemd: the app service ----------------------------------------------
step "Installing services"
cat > "/etc/systemd/system/${APP}.service" <<EOF
[Unit]
Description=${APP} agent (${ROLE})
After=network-online.target avahi-daemon.service
Wants=network-online.target

[Service]
User=${APP_USER}
Environment=EXIMARO_ROLE=${ROLE}
Environment=EXIMARO_DATA=${DATA_DIR}
Environment=EXIMARO_WORK=${WORK_DIR}
Environment=EXIMARO_PORT=${WEB_PORT}
WorkingDirectory=${APP_HOME}
ExecStart=${APP_HOME}/.venv/bin/python -m app
Restart=always
RestartSec=3
# hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${DATA_DIR}
ProtectHome=true
# A private, writable /tmp for LibreOffice's IPC pipe during pptx conversion
# (ProtectSystem=strict makes the real /tmp read-only). The bulk conversion
# scratch still goes to the disk-backed work dir under ${DATA_DIR}.
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
ok "${APP}.service installed (auto-restart, sandboxed, can only write ${DATA_DIR})"

# kiosk: cage launches Chromium fullscreen pointing at the LOCAL screen page.
# the app serves http://localhost:PORT/screen which renders this device's cached playlist.
cat > "/etc/systemd/system/${APP}-kiosk.service" <<EOF
[Unit]
Description=${APP} kiosk display
After=${APP}.service systemd-user-sessions.service getty@tty1.service
Wants=${APP}.service
# Take the console VT away from the login prompt so the kiosk owns the screen.
Conflicts=getty@tty1.service

[Service]
User=${APP_USER}
# A real login session (PAM) is what gives cage a logind seat on seat0 — that
# seat is how wlroots becomes DRM master. Claiming tty1 as the controlling
# terminal is what makes the session "active" so logind hands over the seat.
PAMName=login
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
StandardInput=tty-fail
StandardOutput=journal
StandardError=journal
UtmpIdentifier=tty1
UtmpMode=user
# cage (wlroots) needs XDG_RUNTIME_DIR; enable-linger (above) creates /run/user/<uid>.
# The uid is written in literally: systemd's %U does NOT mean "the User= uid" here —
# it resolves to the service manager's uid (0), so /run/user/%U pointed at root's
# runtime dir, which does not exist and the app user could never write. That also
# aimed the kiosk away from the PipeWire sockets in the app user's own runtime dir.
Environment=XDG_RUNTIME_DIR=/run/user/${APP_UID}
Environment=XDG_SESSION_TYPE=wayland
# Hide the pointer. XCURSOR_PATH is the load-bearing line: it REPLACES the search
# path, so both the compositor and Chromium can only find the transparent cursor
# set built above — pointing it at /usr/share/icons instead lets Chromium reach
# Adwaita and a real arrow appears in the middle of the screen. cage would
# otherwise draw a GPU hardware cursor that ignores the theme entirely, so
# WLR_NO_HARDWARE_CURSORS makes wlroots render the (blank) one instead.
Environment=WLR_NO_HARDWARE_CURSORS=1
Environment=XCURSOR_THEME=default
Environment=XCURSOR_PATH=/usr/share/${APP}/cursors
Environment=XCURSOR_SIZE=24
# Wait until the web app actually answers before launching the browser. systemd
# treats ${APP}.service as "started" the moment the process spawns, but uvicorn
# needs several seconds to import deps and bind the port; without this gate
# Chromium loads too early, gets ERR_CONNECTION_REFUSED, and never retries. On
# timeout this exits non-zero so Restart=always retries the whole unit.
ExecStartPre=/bin/sh -c 'for i in \$(seq 1 60); do curl -sf http://localhost:${WEB_PORT}/healthz >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1'
# --user-data-dir: give Chromium an explicit, writable profile dir. Without one it
# can't place its crash-handler database and aborts on launch
# ("chrome_crashpad_handler: --database is required", SIGABRT/134, restart-looping).
# RuntimeDirectory= makes systemd create /run/${APP}-kiosk owned by User= BEFORE
# ExecStart and remove it on stop: guaranteed to exist with the right owner, with no
# dependency on logind having set up a runtime dir yet, and a throwaway profile suits
# a kiosk. (An earlier version used /run/user/%U here, but %U resolves to 0 — root's
# runtime dir — which the app user cannot write; Chromium then silently fell back to
# a home-dir profile, so it "worked" on any device whose app-user home was writable
# and hard-crash-looped on one where it wasn't.)
RuntimeDirectory=${APP}-kiosk
RuntimeDirectoryMode=0700
ExecStart=/usr/bin/cage -- ${CHROMIUM_PKG} \\
  --kiosk --noerrdialogs --disable-infobars --incognito \\
  --check-for-update-interval=31536000 \\
  --autoplay-policy=no-user-gesture-required \\
  --user-data-dir=/run/${APP}-kiosk/chromium \\
  http://localhost:${WEB_PORT}/screen
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
ok "${APP}-kiosk.service installed (cage + ${CHROMIUM_PKG}, boots straight to fullscreen)"

# The installer is the SOLE owner of the kiosk unit. Wipe any override drop-ins:
# they only ever come from an earlier field hotfix (e.g. a hand-added
# --user-data-dir or --autoplay-policy), and the base unit above already carries
# every needed flag. A leftover drop-in replaces the whole ExecStart and would
# silently mask future changes, so clearing it makes every device (Pi 4, Pi 5,
# x86) converge on exactly this unit on every re-run — no manual cleanup. The
# daemon-reload + kiosk restart near the end of this script apply the result.
rm -rf "/etc/systemd/system/${APP}-kiosk.service.d"

# ---- promotion helper (display -> controller installs conversion packages) ---
# The app runs sandboxed (NoNewPrivileges) and cannot install packages itself.
# When a device is switched to the controller role it drops a request file in the
# data dir; this root-owned path unit notices it and installs the controller
# packages, writing progress back to a status file the UI reads. The package set
# is fixed here, so the app can only ever trigger this one specific install —
# never an arbitrary command.
step "Installing the controller-promotion helper"
cat > "/usr/local/sbin/${APP}-promote" <<EOF
#!/usr/bin/env bash
set -u
DATA_DIR="${DATA_DIR}"
STATUS="\${DATA_DIR}/promote.status"
REQ="\${DATA_DIR}/promote.request"
# rm first + chown -h: this runs as root on a path the unprivileged app user owns,
# so a symlink planted there must not redirect a root write, nor hand its target away.
status(){ rm -f "\$STATUS" 2>/dev/null; printf '{"state":"%s","detail":"%s","when":"%s"}\n' "\$1" "\$2" "\$(date -Is)" >"\$STATUS"; chown -h ${APP_USER}:${APP_USER} "\$STATUS" 2>/dev/null || true; }
status running "installing PowerPoint conversion packages"
export DEBIAN_FRONTEND=noninteractive
if apt-get update -qq && apt-get install -y --no-install-recommends ${CONTROLLER_PKGS}; then
  status done "PowerPoint conversion is ready"
else
  status failed "package install failed (the device needs internet to add PowerPoint support)"
fi
rm -f "\$REQ"
EOF
chmod 0755 "/usr/local/sbin/${APP}-promote"

cat > "/etc/systemd/system/${APP}-promote.service" <<EOF
[Unit]
Description=${APP} controller promotion (install PowerPoint conversion packages)

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/${APP}-promote
EOF

cat > "/etc/systemd/system/${APP}-promote.path" <<EOF
[Unit]
Description=${APP} controller-promotion watcher

[Path]
PathExists=${DATA_DIR}/promote.request
Unit=${APP}-promote.service

[Install]
WantedBy=multi-user.target
EOF
ok "promotion helper installed (watches for a switch to the controller role)"

# ---- audio-output helper (panel picks the output; root applies it) ----------
# The app is sandboxed and can only write ${DATA_DIR}, so it cannot touch
# ${AUDIO_CONF} itself. Same shape as the promote/update helpers: the panel drops a
# request file, this root path unit applies it and writes back the current state for
# the panel to render. The request only ever names an audio output — it can never
# turn into an arbitrary command.
step "Installing the audio-output helper"
cat > "/usr/local/sbin/${APP}-audio" <<EOF
#!/usr/bin/env bash
set -u
DATA_DIR="${DATA_DIR}"; APP_USER="${APP_USER}"; APP_UID="${APP_UID}"
AUDIO_CONF="${AUDIO_CONF}"; HELPER="/usr/local/bin/${APP}-audio-hdmi"
EOF
cat >> "/usr/local/sbin/${APP}-audio" <<'EOF'
REQ="${DATA_DIR}/audio.request"; STATE="${DATA_DIR}/audio.state"

# This runs as ROOT on a file the (unprivileged, sandboxed) app user owns, so treat
# every path as hostile: a symlink planted in the data dir would otherwise turn a
# read into "read any root-only file" and a write into "overwrite any file as root".
# Refuse anything that is not a plain regular file, and never follow a link.
if [ -L "$REQ" ] || { [ -e "$REQ" ] && [ ! -f "$REQ" ]; }; then
  rm -f "$REQ"
  echo "audio: ignoring a request that is not a regular file"
  exit 0
fi

# Read and consume the request up front, so a malformed one can never loop.
WANT="$(head -c 200 "$REQ" 2>/dev/null | tr -d '\r\n')"
rm -f "$REQ"

# Everything audio has to run inside the app user's PipeWire session.
as_app() {
  sudo -u "$APP_USER" env XDG_RUNTIME_DIR="/run/user/${APP_UID}" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${APP_UID}/bus" "$@"
}

case "$WANT" in
  ""|refresh) ;;                                  # just re-read the state below
  auto)
    # Back to choosing automatically. Comment the pin out rather than emptying the
    # file: the comments in it are how someone reading the device over SSH learns
    # what the file is for, and the installer won't re-seed a file that exists.
    [ -f "$AUDIO_CONF" ] && sed -i 's/^[[:space:]]*\([^#[:space:]].*\)$/# (cleared) \1/' "$AUDIO_CONF"
    as_app "$HELPER" >/dev/null 2>&1 ;;
  *)
    # Pin this output, keeping the explanatory comments above it.
    if [ -f "$AUDIO_CONF" ]; then
      sed -i 's/^[[:space:]]*\([^#[:space:]].*\)$/# (replaced) \1/' "$AUDIO_CONF"
    fi
    printf '%s\n' "$WANT" >> "$AUDIO_CONF"
    as_app "$HELPER" >/dev/null 2>&1 ;;
esac

# Publish what the device can actually do, for the panel to render. Built with
# python3 so an output name containing quotes can't produce broken JSON.
OUTPUTS="$(as_app "$HELPER" --raw    2>/dev/null)"
CURRENT="$(as_app "$HELPER" --current 2>/dev/null)"
PINNED="$( as_app "$HELPER" --pin     2>/dev/null)"
# python3 does the writing so it can open the temp file O_NOFOLLOW|O_EXCL: a symlink
# planted at that path by the app user must never become a root-owned write to
# somewhere else. It also publishes only when there is something real to publish, so
# a failed lookup can't blank out a good list.
STATE="$STATE" OUTPUTS="$OUTPUTS" CURRENT="$CURRENT" PINNED="$PINNED" python3 - <<'PY'
import json, os, sys

outputs = []
for line in os.environ.get("OUTPUTS", "").splitlines():
    line = line.strip()
    if not line:
        continue
    sink_id, _, name = line.partition(" ")
    if sink_id.isdigit() and name.strip():
        outputs.append({"id": sink_id, "name": name.strip()})

state = os.environ["STATE"]
payload = json.dumps({
    "outputs": outputs,
    "current": os.environ.get("CURRENT", "").strip(),
    "pinned": os.environ.get("PINNED", "").strip(),
})

tmp = state + ".tmp"
try:
    os.unlink(tmp)
except FileNotFoundError:
    pass
except OSError:
    sys.exit(1)
try:
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
except OSError:
    sys.exit(1)          # someone raced us for that path — publish nothing
with os.fdopen(fd, "w") as fh:
    fh.write(payload)
os.replace(tmp, state)
PY
# chown -h so a symlink at $STATE would be re-owned, never its target.
chown -h "$APP_USER":"$APP_USER" "$STATE" 2>/dev/null || true
EOF
chmod 0755 "/usr/local/sbin/${APP}-audio"

cat > "/etc/systemd/system/${APP}-audio.service" <<EOF
[Unit]
Description=${APP} audio-output change (apply the output chosen in the panel)

[Service]
Type=oneshot
SyslogIdentifier=${APP}-audio
ExecStart=/usr/local/sbin/${APP}-audio
EOF

cat > "/etc/systemd/system/${APP}-audio.path" <<EOF
[Unit]
Description=${APP} audio-output watcher

[Path]
PathExists=${DATA_DIR}/audio.request
Unit=${APP}-audio.service

[Install]
WantedBy=multi-user.target
EOF
# Seed the state now so the panel has something to show on first open.
/usr/local/sbin/${APP}-audio >/dev/null 2>&1 || true
ok "audio-output helper installed (the panel can pick HDMI, headphones, etc.)"

# ---- self-update helper (staged A/B swap with health-checked rollback) -------
# 'Update now' in the UI drops an update.request file; this root path unit builds
# the new version into its own release dir, swaps the ${APP_HOME} symlink to it
# atomically, restarts, and waits for /healthz. If the new version doesn't come up
# healthy it relinks to the previous release and restarts — so a bad update can
# never wedge the device (worst case: still on the previous version). The app is
# sandboxed and only ever writes the request + reads the status it writes back.
step "Installing the self-update helper"
cat > "/usr/local/sbin/${APP}-update" <<EOF
#!/usr/bin/env bash
set -u
APP_USER="${APP_USER}"; APP_HOME="${APP_HOME}"; RELEASES="${RELEASES}"
DATA_DIR="${DATA_DIR}"; WEB_PORT="${WEB_PORT}"; REPO_URL="${REPO_URL}"
REQ="\${DATA_DIR}/update.request"; STATUS="\${DATA_DIR}/update.status"
# rm first + chown -h: root writing into the app user's dir must not follow a symlink.
say(){ rm -f "\$STATUS" 2>/dev/null; printf '{"state":"%s","detail":"%s","when":"%s"}\n' "\$1" "\$2" "\$(date -Is)" >"\$STATUS"; chown -h \${APP_USER}:\${APP_USER} "\$STATUS" 2>/dev/null || true; }
# --no-cache-dir: a wheel truncated by a weak connection stays in pip's cache and gets
# reused on every later build, installing a package that looks present but is broken.
prep_venv(){
  sudo -u \${APP_USER} python3 -m venv --clear "\$1/.venv" >/dev/null 2>&1 || return 1
  sudo -u \${APP_USER} "\$1/.venv/bin/pip" install --quiet --no-cache-dir --upgrade pip >/dev/null 2>&1 || return 1
  sudo -u \${APP_USER} "\$1/.venv/bin/pip" install --quiet --no-cache-dir -r "\$1/requirements.txt" >/dev/null 2>&1 || return 1
}
SRC_REQ="\$(head -c 300 "\$REQ" 2>/dev/null | tr -d '\r\n')"; rm -f "\$REQ"
PREV="\$(readlink -f "\$APP_HOME" 2>/dev/null)"
say running "fetching the new version"
# Source: a local dir override (staging/testing), otherwise a fresh clone of REPO_URL.
if [ -n "\$SRC_REQ" ] && [ -d "\$SRC_REQ" ] && [ -f "\$SRC_REQ/requirements.txt" ]; then
  SRC="\$SRC_REQ"
else
  SRC="\${DATA_DIR}/_update_src"; rm -rf "\$SRC"
  if ! git clone --depth 1 "\$REPO_URL" "\$SRC" >/dev/null 2>&1; then say failed "could not download the update (no internet, or the repository is private)"; exit 0; fi
fi
REL="\${RELEASES}/rel-\$(date +%s)"; rm -rf "\$REL"; mkdir -p "\$REL"
cp -a "\$SRC/." "\$REL/"; rm -rf "\$REL/.venv" "\$REL/.git"; chown -R \${APP_USER}:\${APP_USER} "\$REL"
say running "installing dependencies"
if ! prep_venv "\$REL"; then say failed "could not prepare the new version; staying on the current one"; rm -rf "\$REL"; exit 0; fi
# Same pre-activation gate the installer uses: build the real app object in the new
# venv BEFORE swapping to it, so a broken dependency resolve is caught while the
# working release is still live, instead of becoming a crash loop after activation.
if ! (cd "\$REL" && sudo -u \${APP_USER} "\$REL/.venv/bin/python" -c 'import app.__main__, uvicorn
from app.server import create_app
create_app()' >/dev/null 2>&1); then
  say failed "the new version could not start (a dependency problem) and was not applied — staying on the current one"
  rm -rf "\$REL"; exit 0
fi
say running "switching over"
ln -sfn "\$REL" "\$APP_HOME"; chown -h \${APP_USER}:\${APP_USER} "\$APP_HOME" 2>/dev/null || true
systemctl restart ${APP}.service
# 45s, matching the installer: a Pi takes ~13s to import and bind, and a slow or busy
# device must not be rolled back merely for being slow.
ready=0; for i in \$(seq 1 45); do curl -sf "http://localhost:\${WEB_PORT}/healthz" >/dev/null 2>&1 && { ready=1; break; }; sleep 1; done
if [ "\$ready" = 1 ]; then
  systemctl restart ${APP}-kiosk.service   # reload the kiosk so a new screen.html shows
  say done "updated and verified healthy"
  ls -1dt "\${RELEASES}"/rel-* 2>/dev/null | tail -n +4 | grep -vxF "\$PREV" | xargs -r rm -rf || true
elif [ -n "\$PREV" ] && [ -d "\$PREV" ]; then
  ln -sfn "\$PREV" "\$APP_HOME"; chown -h \${APP_USER}:\${APP_USER} "\$APP_HOME" 2>/dev/null || true
  systemctl restart ${APP}.service; sleep 2; rm -rf "\$REL"
  say rolled_back "the new version did not start; restored the previous version"
else
  say failed "the update did not start and there was no previous version to restore"
fi
EOF
chmod 0755 "/usr/local/sbin/${APP}-update"

cat > "/etc/systemd/system/${APP}-update.service" <<EOF
[Unit]
Description=${APP} self-update (staged swap with health-checked rollback)

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/${APP}-update
EOF

cat > "/etc/systemd/system/${APP}-update.path" <<EOF
[Unit]
Description=${APP} update watcher

[Path]
PathExists=${DATA_DIR}/update.request
Unit=${APP}-update.service

[Install]
WantedBy=multi-user.target
EOF
ok "self-update helper installed (atomic release swap, auto-rollback on failure)"

# ---- WiFi setup helper (privileged nmcli bridge) ----------------------------
# The app is sandboxed and can't touch the network, so WiFi setup (scan nearby
# networks, host the setup network, join a chosen one) is done by this root helper.
# The app drops a JSON request in the data dir; this path unit runs the helper,
# which performs the one action and writes the result back for the app to read.
step "Installing the WiFi setup helper"
install_script "${SRC_DIR}/helpers/eximaro-wifi" "/usr/local/sbin/${APP}-wifi"

# Remote content command: `sudo eximaro-set-url <url>` adds a link (Google Slides
# deck, page, or video) to a device and pushes it out on a controller; --replace
# wipes the current content first. Handy for managing a fleet over SSH / an RMM.
install_script "${SRC_DIR}/helpers/eximaro-set-url" "/usr/local/sbin/${APP}-set-url"

# Captive portal for the setup network: NetworkManager's shared-mode dnsmasq resolves
# every hostname to the gateway (10.42.0.1) so a joined phone's connectivity probe
# lands on the app, which redirects it to the setup page — that is what makes phones
# auto-open it. This file is read ONLY by the hotspot dnsmasq; a normal WiFi client
# never starts that instance, so it is inert in client mode and safe to leave here.
# (The :80->:8080 redirect that pairs with it is added by the helper only while the
# AP is up, and torn down the moment it drops.)
install -d -m 0755 /etc/NetworkManager/dnsmasq-shared.d
cat > "/etc/NetworkManager/dnsmasq-shared.d/00-${APP}-captive.conf" <<'EOF'
# Point every hostname at the setup gateway so captive-portal probes reach us.
address=/#/10.42.0.1
EOF
ok "captive-portal DNS installed (setup network only)"

# A one-command full updater: pulls the latest from GitHub and re-runs install.sh
# (delivers the app AND these system pieces, unlike the in-app "Update now").
install_script "${SRC_DIR}/update.sh" "/usr/local/sbin/${APP}-update-full"

cat > "/etc/systemd/system/${APP}-wifi.service" <<EOF
[Unit]
Description=${APP} WiFi setup helper (runs nmcli for the sandboxed app)
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
Environment=DATA_DIR=${DATA_DIR}
Environment=APP_USER=${APP_USER}
ExecStart=/usr/local/sbin/${APP}-wifi
EOF

cat > "/etc/systemd/system/${APP}-wifi.path" <<EOF
[Unit]
Description=${APP} WiFi request watcher

[Path]
PathExists=${DATA_DIR}/wifi.request
Unit=${APP}-wifi.service

[Install]
WantedBy=multi-user.target
EOF
ok "WiFi setup helper installed (scan, host the setup network, join a chosen one)"

# ---- self-recovery: watchdog + panic reboot + app liveness ------------------
# A crashed service already restarts (Restart=always) and a power cut already
# comes back (units enabled at boot). Three gaps remain, closed here — every one
# chosen to FAIL SAFE, because these boxes are unattended and remote-only and a
# reboot loop nobody can power-cycle is the worst possible outcome:
#
#  1. A wedged init (PID 1 hung) OR a fully frozen kernel: nothing is left to restart
#     anything. systemd pets the board's hardware watchdog every RuntimeWatchdogSec/2;
#     if it stops (init hung, kernel frozen) the timer expires and RESETS the box in
#     hardware. On a Pi that watchdog is off until enabled in the firmware config, so
#     we enable it there. We do NOT use a software watchdog: it can't fire once the
#     kernel is dead, and persisting it would shadow the real one. Heavy work like an
#     update never trips it: that runs in child processes; PID 1 stays responsive.
#
#  2. A true kernel PANIC: default is to hang forever. kernel.panic=10 reboots
#     after 10s instead — a transient panic then recovers on its own. We deliberately
#     DO NOT set panic_on_oops: an oops is usually survivable and leaves the box
#     REACHABLE to fix, whereas rebooting on every oops would turn one repeatable
#     driver oops into an unrecoverable reboot loop on a device no one can reach.
#
#  3. An app that is ALIVE but wedged (uvicorn stops answering without exiting):
#     Restart=always never fires (no exit) and the init watchdog never fires (PID 1
#     is fine). A tiny timer curls /healthz and restarts ONLY the app service after
#     it has failed several checks in a row — a restart, never a reboot, and only
#     once the service claims to be running, so it can't fight a slow boot.
step "Setting up self-recovery (watchdog + panic reboot + liveness)"

# (1) init watchdog — the board's REAL hardware watchdog (a Pi's bcm2835, an x86
# board's iTCO). Only hardware can reset a fully frozen kernel; a software watchdog
# can't fire once the kernel is dead, so we do NOT use softdog — persisting it would
# also let it shadow a hardware watchdog enabled later (it wins /dev/watchdog by load
# order). On a Pi OS trixie image the hw watchdog is off until dtparam=watchdog=on, so
# enable it in the firmware config (idempotent); it arms after the next reboot.
# (1) NO hardware watchdog. An earlier version of this installer armed systemd's
# watchdog (RuntimeWatchdogSec) and enabled dtparam=watchdog=on. On a real Pi 5 that
# REBOOTED THE DEVICE ROUGHLY EVERY 20 SECONDS — the exact unattended-brick this was
# meant to prevent. A watchdog that resets a healthy machine is far worse than the
# rare freeze it guards against, and this appliance already recovers from every
# failure that actually happens in the field: services restart on crash, units start
# at boot after a power cut, and the liveness timer below restarts a wedged app.
# Do not re-introduce a watchdog without proving it on hardware over hours, not
# minutes. Actively REMOVE it from any device a previous version armed:
rm -f "/etc/systemd/system.conf.d/${APP}-watchdog.conf" \
      "/etc/modules-load.d/${APP}-watchdog.conf" \
      "/etc/sysctl.d/90-${APP}-recovery.conf"
for _c in /boot/firmware/config.txt /boot/config.txt; do
  if [ -f "$_c" ] && grep -qE "^[[:space:]]*dtparam=watchdog=on" "$_c"; then
    sed -i "/^# ${APP}: hardware watchdog/d; /^[[:space:]]*dtparam=watchdog=on[[:space:]]*$/d" "$_c"
    warn "removed the hardware-watchdog line this installer previously added to $_c"
  fi
done
sysctl -w kernel.panic=0 >/dev/null 2>&1 || true
# Re-exec init so a previously-armed watchdog is disarmed NOW, not at the next boot.
systemctl daemon-reexec >/dev/null 2>&1 || true
ok "no watchdog (a previous version's watchdog is removed if present)"

# (3) app-liveness watchdog: restart the app if /healthz stops answering while the
# service is up. Conservative on purpose — 3 misses (~3 min) before it acts.
cat > "/usr/local/sbin/${APP}-healthcheck" <<EOF
#!/bin/sh
set -u
FAILS="/run/${APP}-healthcheck.fails"
# Only judge the app while systemd thinks it is running — never during a (slow) boot
# or a deliberate stop, so this can't restart-loop a starting or stopped service.
[ "\$(systemctl is-active ${APP}.service)" = "active" ] || { rm -f "\$FAILS"; exit 0; }
if curl -sf --max-time 10 "http://localhost:${WEB_PORT}/healthz" >/dev/null 2>&1; then
  rm -f "\$FAILS"; exit 0
fi
N=\$(( \$(cat "\$FAILS" 2>/dev/null || echo 0) + 1 )); echo "\$N" > "\$FAILS"
[ "\$N" -lt 3 ] && exit 0                 # tolerate a couple of blips
rm -f "\$FAILS"
logger -t ${APP}-healthcheck "app unresponsive on /healthz after \$N checks — restarting ${APP}.service"
systemctl restart ${APP}.service
EOF
chmod 0755 "/usr/local/sbin/${APP}-healthcheck"
cat > "/etc/systemd/system/${APP}-healthcheck.service" <<EOF
[Unit]
Description=${APP} app-liveness check (restart the app if /healthz stops answering)
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/${APP}-healthcheck
EOF
cat > "/etc/systemd/system/${APP}-healthcheck.timer" <<EOF
[Unit]
Description=${APP} app-liveness check, every minute
[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
EOF

ok "self-recovery: services restart on crash, start at boot, and a wedged app restarts"

# (4) don't let the power button turn the sign off. On Raspberry Pi OS Lite a single
# short press of the Pi 5's power button is an IMMEDIATE shutdown, and a Pi 5 then
# STAYS OFF until someone presses it again — a bumped button, a resting cable, or a
# curious hand kills a wall-mounted sign for as long as nobody notices. Tell logind to
# ignore the key so a press can't shut it down. A deliberate ~10s hold still hard-offs
# at the PMIC (firmware, below logind), and pressing it while off still boots — so the
# button stays useful for the operator, just not as an accidental kill switch.
install -d -m 0755 /etc/systemd/logind.conf.d
cat > "/etc/systemd/logind.conf.d/${APP}-powerkey.conf" <<'EOF'
[Login]
HandlePowerKey=ignore
HandlePowerKeyLongPress=ignore
EOF
# NOT applied live: restarting systemd-logind can drop the kiosk's seat. It takes
# effect on the next reboot, which is enough — it only needs to be armed before the
# NEXT accidental press, not this instant.
ok "power button set to ignore (takes effect after the next reboot; a ~10s hold still forces off)"

systemctl daemon-reload
systemctl enable "${APP}.service" "${APP}-kiosk.service" "${APP}-promote.path" "${APP}-update.path" "${APP}-wifi.path" "${APP}-audio.path" "${APP}-healthcheck.timer" >/dev/null 2>&1 || true
# Use restart (not just enable --now) so re-running the installer to UPDATE
# actually loads the new code — enable --now is a no-op on an already-running unit.
systemctl restart "${APP}.service"       >/dev/null 2>&1 || warn "could not start ${APP}.service yet (app code may be incomplete)"
systemctl restart "${APP}-kiosk.service" >/dev/null 2>&1 || warn "could not start kiosk yet"
systemctl restart "${APP}-promote.path"  >/dev/null 2>&1 || true
systemctl restart "${APP}-update.path"   >/dev/null 2>&1 || true
systemctl restart "${APP}-wifi.path"     >/dev/null 2>&1 || true
systemctl restart "${APP}-audio.path"    >/dev/null 2>&1 || true
# Start the liveness timer NOW, not just at the next boot — 'enable' alone only wires
# it into timers.target, which already fired this boot before the installer ran.
systemctl restart "${APP}-healthcheck.timer" >/dev/null 2>&1 || true

# ---- verify the new release actually serves; roll back if it doesn't --------
# The import check above catches a broken build before activation. This second gate
# catches a build that imports but won't come up (a startup crash, a bad config, a
# port it can't bind): wait for /healthz, and if it never answers, relink to the
# release that was active before this run and restart. A remote, unattended device
# must never be left crash-looping — same safety the in-app updater already has.
if [ "$CHECK_ONLY" != 1 ]; then
  step "Verifying the new version is healthy"
  _healthy=0
  for _i in $(seq 1 45); do
    curl -sf "http://localhost:${WEB_PORT}/healthz" >/dev/null 2>&1 && { _healthy=1; break; }
    sleep 1
  done
  if [ "$_healthy" = 1 ]; then
    ok "new version is up and answering /healthz"
    [ -n "${PREV_REL:-}" ] && update_status done "Updated successfully."
    # Only now that this release is proven healthy: keep the newest few for rollback
    # and prune the rest, never touching the one we'd roll back to.
    ls -1dt "${RELEASES}"/rel-* 2>/dev/null | tail -n +4 \
      | { [ -n "${PREV_REL:-}" ] && grep -vxF "$PREV_REL" || cat; } | xargs -r rm -rf || true
  elif [ -n "${PREV_REL:-}" ] && [ -d "$PREV_REL" ]; then
    warn "the new version did not answer /healthz — rolling back to the previous release"
    ln -sfn "$PREV_REL" "$APP_HOME"; chown -h "$APP_USER":"$APP_USER" "$APP_HOME" 2>/dev/null || true
    systemctl restart "${APP}.service" >/dev/null 2>&1 || true
    for _i in $(seq 1 30); do curl -sf "http://localhost:${WEB_PORT}/healthz" >/dev/null 2>&1 && break; sleep 1; done
    systemctl restart "${APP}-kiosk.service" >/dev/null 2>&1 || true
    update_status rolled_back "The last update failed to start and was rolled back — this device is on its previous, working version. Try again on a stronger connection."
    rm -rf "$REL"
    die "update did not start; rolled back to ${PREV_REL}. The device is on its previous, working version."
  else
    warn "the new version did not answer /healthz, and there is no previous release to roll back to."
    update_status failed "The last update did not start and there was no previous version to fall back to. This device needs attention."
    warn "  Check what failed:  sudo journalctl -u ${APP}.service -n 40 --no-pager"
  fi
fi

# ---- done -------------------------------------------------------------------
step "Done"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "Role:        ${ROLE}"
echo "App:         ${APP_HOME}"
echo "Data/secrets:${DATA_DIR} (0700)"
if [ "$ROLE" = "controller" ]; then
  echo "Control panel: http://${IP:-<this-device-ip>}:${WEB_PORT}/"
else
  echo "This display:  http://${IP:-<this-device-ip>}:${WEB_PORT}/  (shows its pairing code when you start pairing)"
fi
echo
echo "Re-run diagnostics anytime:  sudo ./install.sh --check"
