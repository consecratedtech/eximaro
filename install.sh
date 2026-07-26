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

# Resolve a pin (a numeric sink id, or a case-insensitive name fragment) to an id,
# echoing nothing when it matches no CURRENT sink.
resolve() {
  case "$1" in
    ''|*[!0-9]*) list_sinks | LC_ALL=C grep -iE -- "$1" | awk '{ print $1; exit }' ;;
    *)           list_sinks | awk -v want="$1" '$1 == want { print $1; exit }' ;;
  esac
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
# Never overwrite an operator's existing pin.
install -d -m 0755 "$(dirname "${AUDIO_CONF}")"
if [ ! -f "${AUDIO_CONF}" ]; then
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
# An eximaro screen has no operator, so the pointer must never show. cage ignores
# XCURSOR_THEME and loads the system "default" xcursor theme regardless, so we
# build a "blank" theme whose cursors are 1x1 fully-transparent images, alias
# every common cursor name to it, then repoint /usr/share/icons/default at it.
# (Pairing this with software cursors in the kiosk unit makes the blank theme
# actually take effect — see the WLR_NO_HARDWARE_CURSORS note below.)
step "Hiding the mouse cursor (blank xcursor theme)"
install -d -m 0755 /usr/share/icons/blank/cursors
python3 - <<'PY'
import struct
sizes=[16,24,32,48,64]; px=struct.pack('<I',0)
chunks=[struct.pack('<IIIIIIIII',36,0xfffd0002,s,1,1,1,0,0,0)+px for s in sizes]
hdr=b'Xcur'+struct.pack('<III',16,0x00010000,len(chunks))
pos=16+len(chunks)*12; offs=[]
for c in chunks: offs.append(pos); pos+=len(c)
toc=b''.join(struct.pack('<III',0xfffd0002,s,o) for s,o in zip(sizes,offs))
open('/usr/share/icons/blank/cursors/left_ptr','wb').write(hdr+toc+b''.join(chunks))
PY
( cd /usr/share/icons/blank/cursors
  for n in default pointer arrow top_left_arrow left_ptr_watch xterm text ibeam hand hand1 hand2 pointing_hand grab grabbing openhand closedhand watch wait progress crosshair cross fleur move all-scroll col-resize row-resize size_all size_ver size_hor n-resize e-resize s-resize w-resize ne-resize nw-resize se-resize sw-resize not-allowed no-drop forbidden help question_arrow context-menu copy alias; do
    ln -sf left_ptr "$n"
  done )
printf '[Icon Theme]\nName=blank\nComment=Invisible cursor for kiosk\n' > /usr/share/icons/blank/index.theme
install -d -m 0755 /usr/share/icons/default
ln -sfn /usr/share/icons/blank/cursors /usr/share/icons/default/cursors
ok "blank cursor theme installed and set as the system default"

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
sudo -u "$APP_USER" "$REL/.venv/bin/pip" install --quiet --upgrade pip
if [ -f "$REL/requirements.txt" ]; then
  sudo -u "$APP_USER" "$REL/.venv/bin/pip" install --quiet -r "$REL/requirements.txt"
  ok "python dependencies installed in venv"
else
  warn "no requirements.txt — skipping pip install"
fi

# Activate this release. An older FLAT install (a real dir, not a symlink) is
# retired into ${RELEASES} first so it remains as a rollback target.
if [ -e "$APP_HOME" ] && [ ! -L "$APP_HOME" ]; then
  rm -rf "${RELEASES}/rel-preexisting"
  mv "$APP_HOME" "${RELEASES}/rel-preexisting" 2>/dev/null || rm -rf "$APP_HOME"
fi
ln -sfn "$REL" "$APP_HOME"          # atomic activate
chown -h "$APP_USER":"$APP_USER" "$APP_HOME" 2>/dev/null || true
ok "activated ${APP_HOME} -> ${REL}"
# Keep the newest few releases for rollback; prune older ones.
ls -1dt "${RELEASES}"/rel-* 2>/dev/null | tail -n +4 | xargs -r rm -rf || true

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
# Hide the pointer: software cursors + the blank "default" theme (installed above)
# make it invisible. cage otherwise draws a GPU hardware cursor that ignores the
# theme, so WLR_NO_HARDWARE_CURSORS forces wlroots to render the (blank) cursor.
Environment=WLR_NO_HARDWARE_CURSORS=1
Environment=XCURSOR_THEME=blank
Environment=XCURSOR_PATH=/usr/share/icons
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
status(){ printf '{"state":"%s","detail":"%s","when":"%s"}\n' "\$1" "\$2" "\$(date -Is)" >"\$STATUS"; chown ${APP_USER}:${APP_USER} "\$STATUS" 2>/dev/null || true; }
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
  auto)  : > "$AUDIO_CONF"                        # clear the pin -> automatic HDMI
         as_app "$HELPER" >/dev/null 2>&1 ;;
  *)     printf '%s\n' "$WANT" > "$AUDIO_CONF"    # pin this output
         as_app "$HELPER" >/dev/null 2>&1 ;;
esac

# Publish what the device can actually do, for the panel to render. Built with
# python3 so an output name containing quotes can't produce broken JSON.
OUTPUTS="$(as_app "$HELPER" --raw    2>/dev/null)"
CURRENT="$(as_app "$HELPER" --current 2>/dev/null)"
PINNED="$( as_app "$HELPER" --pin     2>/dev/null)"
OUTPUTS="$OUTPUTS" CURRENT="$CURRENT" PINNED="$PINNED" python3 - <<'PY' > "$STATE".tmp
import json, os
outputs = []
for line in os.environ.get("OUTPUTS", "").splitlines():
    line = line.strip()
    if not line:
        continue
    sink_id, _, name = line.partition(" ")
    if sink_id.isdigit():
        outputs.append({"id": sink_id, "name": name.strip()})
print(json.dumps({
    "outputs": outputs,
    "current": os.environ.get("CURRENT", "").strip(),
    "pinned": os.environ.get("PINNED", "").strip(),
}))
PY
mv -f "$STATE".tmp "$STATE"
chown "$APP_USER":"$APP_USER" "$STATE" 2>/dev/null || true
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
say(){ printf '{"state":"%s","detail":"%s","when":"%s"}\n' "\$1" "\$2" "\$(date -Is)" >"\$STATUS"; chown \${APP_USER}:\${APP_USER} "\$STATUS" 2>/dev/null || true; }
prep_venv(){
  sudo -u \${APP_USER} python3 -m venv --clear "\$1/.venv" >/dev/null 2>&1 || return 1
  sudo -u \${APP_USER} "\$1/.venv/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || return 1
  sudo -u \${APP_USER} "\$1/.venv/bin/pip" install --quiet -r "\$1/requirements.txt" >/dev/null 2>&1 || return 1
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
say running "switching over"
ln -sfn "\$REL" "\$APP_HOME"; chown -h \${APP_USER}:\${APP_USER} "\$APP_HOME" 2>/dev/null || true
systemctl restart ${APP}.service
ready=0; for i in \$(seq 1 30); do curl -sf "http://localhost:\${WEB_PORT}/healthz" >/dev/null 2>&1 && { ready=1; break; }; sleep 1; done
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
install -m 0755 "${SRC_DIR}/helpers/eximaro-wifi" "/usr/local/sbin/${APP}-wifi"

# Remote content command: `sudo eximaro-set-url <url>` adds a link (Google Slides
# deck, page, or video) to a device and pushes it out on a controller; --replace
# wipes the current content first. Handy for managing a fleet over SSH / an RMM.
install -m 0755 "${SRC_DIR}/helpers/eximaro-set-url" "/usr/local/sbin/${APP}-set-url"

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
install -m 0755 "${SRC_DIR}/update.sh" "/usr/local/sbin/${APP}-update-full"

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

systemctl daemon-reload
systemctl enable "${APP}.service" "${APP}-kiosk.service" "${APP}-promote.path" "${APP}-update.path" "${APP}-wifi.path" "${APP}-audio.path" >/dev/null 2>&1 || true
# Use restart (not just enable --now) so re-running the installer to UPDATE
# actually loads the new code — enable --now is a no-op on an already-running unit.
systemctl restart "${APP}.service"       >/dev/null 2>&1 || warn "could not start ${APP}.service yet (app code may be incomplete)"
systemctl restart "${APP}-kiosk.service" >/dev/null 2>&1 || warn "could not start kiosk yet"
systemctl restart "${APP}-promote.path"  >/dev/null 2>&1 || true
systemctl restart "${APP}-update.path"   >/dev/null 2>&1 || true
systemctl restart "${APP}-wifi.path"     >/dev/null 2>&1 || true
systemctl restart "${APP}-audio.path"    >/dev/null 2>&1 || true

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
