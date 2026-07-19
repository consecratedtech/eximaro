# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
"""Configuration: paths from the environment the installer sets, plus a small
persisted config file (role + display name) living in the data dir.

The role is seeded from EXIMARO_ROLE on first run, but once written to
config.json it is owned by the app — so it can be changed at any time from the
UI without touching the installer. A device can switch between controller and
display roles whenever needed.
"""

import json
import os
from pathlib import Path

def _default_data_dir() -> Path:
    """Production uses /var/lib/eximaro (the installer sets EXIMARO_DATA).
    For local dev without sudo, fall back to a user-writable dir so
    `python -m app` just works."""
    env = os.environ.get("EXIMARO_DATA")
    if env:
        return Path(env)
    system = Path("/var/lib/eximaro")
    # Use the production dir if it already exists and we can write it (the service
    # user owns it, 0700), or if we could create it (running as root / installer).
    # Checking the dir ITSELF — not just its parent — is what lets the unprivileged
    # service user resolve the right dir with no env set, e.g. the `reset-password`
    # recovery command run as `eximaro` (/var/lib is root-owned, so the old
    # parent-only check sent it to ~/.local and silently cleared the wrong vault).
    if (system.is_dir() and os.access(system, os.W_OK)) or os.access(system.parent, os.W_OK):
        return system
    return Path.home() / ".local" / "share" / "eximaro"


DATA = _default_data_dir()
WORK = Path(os.environ.get("EXIMARO_WORK", str(DATA / "work")))
PORT = int(os.environ.get("EXIMARO_PORT", "8080"))

# Release/build identifier — surfaced in /healthz. Bump on each tagged release.
BUILD_TAG = "emx-b1-9f3k7q2x8v4"

CONFIG_PATH = DATA / "config.json"
VALID_ROLES = {"controller", "display"}


def _seed_from_env() -> dict:
    role = os.environ.get("EXIMARO_ROLE")
    if role not in VALID_ROLES:
        role = None  # unconfigured -> first-boot splash
    return {"role": role, "name": None, "sync_hostname": True, "shuffle": False}


def load_config() -> dict:
    cfg = _seed_from_env()
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except (json.JSONDecodeError, OSError):
            pass  # corrupt/unreadable -> fall back to env defaults
    if cfg.get("role") not in VALID_ROLES:
        cfg["role"] = None
    return cfg


def save_config(cfg: dict) -> dict:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.replace(tmp, CONFIG_PATH)  # atomic
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
    return cfg
