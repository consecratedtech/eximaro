#!/usr/bin/env bash
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
#
# run.sh — try it locally in one command (no sudo, no device needed).
# Sets up a virtualenv, installs deps, and starts the app.
#
#   ./run.sh
#   then open http://localhost:8080
#
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if [ ! -d .venv ]; then
  echo "Creating virtualenv..."
  "$PYTHON" -m venv .venv
fi

# shellcheck disable=SC1091
. .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo
echo "Starting on http://localhost:${EXIMARO_PORT:-8080}  (Ctrl+C to stop)"
echo
exec python -m app
