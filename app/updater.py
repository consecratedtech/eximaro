# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
"""Self-update: ask the privileged helper to install a new app release.

The app is sandboxed and can't replace its own files, so "Update now" just drops
a request file in the data dir. A root-owned path unit (installed by install.sh)
builds the new version into its own release dir, swaps the /opt/eximaro symlink to
it atomically, restarts, and waits for /healthz — rolling the symlink back to the
previous release if the new one doesn't come up healthy. The app only ever writes
the request and reads the status the helper writes back, so a bad update can never
wedge the device.
"""

import json

from . import config, __version__

REQUEST_PATH = config.DATA / "update.request"
STATUS_PATH = config.DATA / "update.status"


def current_version() -> str:
    return __version__


def request_update(source: str = "") -> None:
    """Trigger an update. `source` is normally empty (fetch the latest release);
    a local directory path may be passed for staging/testing."""
    try:
        config.DATA.mkdir(parents=True, exist_ok=True)
        REQUEST_PATH.write_text((source.strip() or "latest") + "\n")
    except OSError:
        pass


def in_progress() -> bool:
    return REQUEST_PATH.exists()


def status() -> dict:
    """The helper's last-written status: {state, detail, when}, or {} if none."""
    try:
        return json.loads(STATUS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
