#!/usr/bin/env bash
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
#
# Move an already-running eximaro device onto the new repository.
#
# Ships the new code to the device over SSH as a source tarball, so this works
# while the repo is still PRIVATE — no GitHub token is ever written to the
# device. The device's baked-in REPO_URL is rewritten in the same pass, so the
# moment the repo goes public the normal in-app "Update now" pulls from the new
# home with nothing further to do here.
#
# Everything in /var/lib/eximaro (library, config, device id, keys, pairings) is
# backed up first and is never touched by the install — the device keeps its
# role, its content, and its paired displays.
#
# Usage:
#   ./tools/migrate-device.sh user@host [-i ~/.ssh/key]
#   ./tools/migrate-device.sh user@host -i ~/.ssh/key --dry-run
#   ./tools/migrate-device.sh user@host --repo-url URL
#
# Re-runnable: running it twice is harmless (the installer is idempotent), and a
# device already on the new build is detected and skipped.

set -euo pipefail

# ---- settings ---------------------------------------------------------------
NEW_REPO_URL="https://github.com/consecratedtech/eximaro.git"
APP="eximaro"
APP_HOME="/opt/${APP}"
DATA_DIR="/var/lib/${APP}"
WEB_PORT="8080"

DRY_RUN=0
TARGET=""
IDENTITY="${EXIMARO_SSH_KEY:-}"

# ---- pretty output ----------------------------------------------------------
c() { printf '\033[%sm%s\033[0m' "$1" "$2"; }
ok()   { echo "$(c '0;32' '  ok ') $*"; }
warn() { echo "$(c '0;33' 'warn ') $*"; }
die()  { echo "$(c '0;31' 'FAIL ') $*" >&2; exit 1; }
step() { echo; echo "$(c '1;36' "==> $*")"; }

# ---- args -------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)      DRY_RUN=1; shift ;;
    --repo-url)     NEW_REPO_URL="${2:-}"; shift 2 ;;
    -i|--identity)  IDENTITY="${2:-}"; shift 2 ;;
    -*)             die "unknown argument: $1" ;;
    *)              TARGET="$1"; shift ;;
  esac
done
[ -n "$TARGET" ] || die "usage: $0 user@host [-i identity_file] [--dry-run] [--repo-url URL]"

# Default SSH only offers the appliance key when it is named explicitly.
SSH_OPTS=(-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)
[ -n "$IDENTITY" ] && SSH_OPTS+=(-i "$IDENTITY")
rsh() { ssh "${SSH_OPTS[@]}" "$TARGET" "$@"; }

# ---- 0. local pre-flight ----------------------------------------------------
# Confirm we are standing in the NEW repo and not the old GPL checkout — shipping
# the wrong tree would quietly migrate a device backwards.
step "Local pre-flight"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

[ -f "app/config.py" ] || die "not an eximaro checkout: $REPO_ROOT"
grep -q "PolyForm Noncommercial License" LICENSE 2>/dev/null \
  || die "this checkout is not the relicensed tree (LICENSE is not PolyForm). Run from the new repo."

# The build identifier the migrated device must report back. Read from source so
# it can never drift out of sync with a hardcoded copy here.
EXPECT_BUILD="$(sed -n 's/^BUILD_TAG *= *"\(.*\)"/\1/p' app/config.py | head -n1)"
[ -n "$EXPECT_BUILD" ] || die "could not read BUILD_TAG from app/config.py"
ok "source tree : $REPO_ROOT"
ok "target build: ${EXPECT_BUILD}"
git diff --quiet 2>/dev/null || warn "uncommitted changes will NOT ship — only committed HEAD is sent; commit first to include them"

# ---- 1. remote pre-flight ---------------------------------------------------
step "Remote pre-flight (${TARGET})"
rsh 'true' || die "cannot reach ${TARGET} over SSH"

# One round trip for everything we need. Quoted heredoc: nothing expands locally,
# so there is no escaping hazard between the two shells.
FACTS="$(rsh 'sudo bash -s' <<'REMOTE'
set -u
D=/var/lib/eximaro
echo "release=$(readlink -f /opt/eximaro 2>/dev/null || echo none)"
echo "data=$([ -d "$D" ] && echo yes || echo no)"
echo "build=$(curl -sf http://localhost:8080/healthz 2>/dev/null | sed -n 's/.*"build" *: *"\([^"]*\)".*/\1/p')"
echo "arch=$(uname -m)"
# Role of record is config.json; on a device that never switched roles that file
# does not exist yet and the seeded EXIMARO_ROLE in the unit is authoritative.
ROLE="$(python3 -c 'import json;print(json.load(open("/var/lib/eximaro/config.json")).get("role") or "")' 2>/dev/null || true)"
if [ -z "$ROLE" ]; then
  ROLE="$(sed -n 's/^Environment=EXIMARO_ROLE=\(.*\)$/\1/p' /etc/systemd/system/eximaro.service 2>/dev/null | head -n1)"
  echo "role_src=unit"
else
  echo "role_src=config.json"
fi
echo "role=$ROLE"
REMOTE
)"

# Parse explicit fields — never eval remote output.
fact() { printf '%s\n' "$FACTS" | sed -n "s/^$1=//p" | head -n1; }
R_release="$(fact release)"; R_data="$(fact data)"; R_build="$(fact build)"
R_arch="$(fact arch)";       R_role="$(fact role)"; R_role_src="$(fact role_src)"

[ "$R_data" = "yes" ]      || die "no ${DATA_DIR} on the device — this is not an installed eximaro box"
[ "$R_release" != "none" ] || die "no active release at ${APP_HOME}"

ok "arch            : ${R_arch}"
ok "current release : ${R_release}"
ok "current role    : ${R_role:-<none>}  (from ${R_role_src:-?})"
ok "current build   : ${R_build:-<none reported — pre-migration code>}"

if [ "$R_build" = "$EXPECT_BUILD" ]; then
  ok "device already reports the new build — nothing to migrate."
  exit 0
fi

# The installer prompts interactively when it cannot infer a role. Refuse to
# guess: guessing wrong would silently flip a controller into a display.
case "$R_role" in
  controller|display) ROLE_ARG="--role ${R_role}" ;;
  *) die "could not determine the device role (config.json absent and no EXIMARO_ROLE in the unit).
       Re-run with the role forced, e.g.:  EXIMARO_FORCE_ROLE=display $0 $TARGET" ;;
esac
[ -n "${EXIMARO_FORCE_ROLE:-}" ] && ROLE_ARG="--role ${EXIMARO_FORCE_ROLE}"

if [ "$DRY_RUN" = 1 ]; then
  echo
  ok "dry run — would ship $(git rev-parse --short HEAD) and run: install.sh ${ROLE_ARG}"
  ok "dry run — would set future update source to: ${NEW_REPO_URL}"
  ok "nothing on the device was changed."
  exit 0
fi

# ---- 2. build the source tarball -------------------------------------------
step "Building source tarball from HEAD"
STAMP="$(git rev-parse --short HEAD)"
TARBALL="$(mktemp -t eximaro-src-XXXXXX).tar.gz"
git archive --format=tar.gz -o "$TARBALL" HEAD
ok "packed ${STAMP} ($(wc -c <"$TARBALL" | tr -d ' ') bytes)"

# ---- 3. back up device data -------------------------------------------------
# Taken BEFORE anything is touched. The install does not write to DATA_DIR, but a
# restore point costs seconds and turns a bad night into a two-minute fix.
step "Backing up device data"
BACKUP="/root/${APP}-predata-${STAMP}.tgz"
rsh "sudo tar czf '${BACKUP}' -C /var/lib eximaro"
ok "data backed up on the device: ${BACKUP}"
ok "rollback release recorded  : ${R_release}"

# ---- 4. ship + install ------------------------------------------------------
step "Shipping code and running the installer (several minutes — it rebuilds the venv)"
scp "${SSH_OPTS[@]}" "$TARBALL" "${TARGET}:/tmp/eximaro-src.tar.gz" >/dev/null
rm -f "$TARBALL"

# REPO_URL is exported into the installer, which bakes it into the device's
# self-update helper (/usr/local/sbin/eximaro-update). That is the line that
# re-points all FUTURE in-app updates at the new repository.
# `-s --` is required: without the separator bash parses --role as its own option.
rsh "sudo env REPO_URL='${NEW_REPO_URL}' bash -s -- ${ROLE_ARG}" <<'REMOTE'
set -e
rm -rf /tmp/eximaro-migrate && mkdir -p /tmp/eximaro-migrate
tar xzf /tmp/eximaro-src.tar.gz -C /tmp/eximaro-migrate
cd /tmp/eximaro-migrate
chmod +x install.sh
./install.sh "$@"
REMOTE
ok "installer finished"

# ---- 5. verify --------------------------------------------------------------
# The device must come back up AND report the new build identifier. Anything else
# counts as a failed migration.
step "Verifying"
NEW_BUILD=""
for _ in $(seq 1 30); do
  NEW_BUILD="$(rsh "curl -sf http://localhost:${WEB_PORT}/healthz 2>/dev/null" \
    | sed -n 's/.*"build" *: *"\([^"]*\)".*/\1/p')" || true
  [ -n "$NEW_BUILD" ] && break
  sleep 2
done

if [ "$NEW_BUILD" = "$EXPECT_BUILD" ]; then
  ok "healthz reports ${NEW_BUILD} — migration verified"
else
  warn "device reported '${NEW_BUILD:-nothing}', expected '${EXPECT_BUILD}'"
  step "Rolling back to ${R_release}"
  rsh "sudo sh -c 'ln -sfn ${R_release} ${APP_HOME} && systemctl restart ${APP}.service'" || true
  die "migration failed and the previous release was restored. Data backup: ${BACKUP}"
fi

# The kiosk caches /screen at boot; restart it so the display runs the new page.
rsh "sudo systemctl restart ${APP}-kiosk.service" || warn "could not restart the kiosk — reboot the device"

# Confirm the self-update helper now points at the new repository.
BAKED="$(rsh "sudo grep -o 'REPO_URL=\"[^\"]*\"' /usr/local/sbin/${APP}-update | head -n1" || true)"
case "$BAKED" in
  *consecratedtech/eximaro*) ok "self-update now points at the new repository" ;;
  *) warn "self-update still reads: ${BAKED:-<unreadable>}" ;;
esac

rsh "sudo rm -rf /tmp/eximaro-migrate /tmp/eximaro-src.tar.gz" || true

step "Done"
ok "${TARGET} is on the new code and will pull future updates from the new repository."
echo "   Data backup left on the device at ${BACKUP} (delete once you are happy)."
echo "   While the repo is PRIVATE, in-app 'Update now' cannot clone it — re-run"
echo "   this script to push updates until the repo goes public."
