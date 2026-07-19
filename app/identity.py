# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
"""Device identity.

The Device ID is the permanent, immutable anchor for the whole trust model:
generated exactly once on first boot, never changed. Pairing and command
verification key off this — NOT off the (changeable) display name. That is what
lets a device be renamed freely without breaking any pairing.
"""

import os
import secrets

from . import config

DEVICE_ID_PATH = config.DATA / "device_id"


def get_or_create_device_id() -> str:
    if DEVICE_ID_PATH.exists():
        existing = DEVICE_ID_PATH.read_text().strip()
        if existing:
            return existing
    config.DATA.mkdir(parents=True, exist_ok=True)
    device_id = secrets.token_hex(16)  # 128-bit, unguessable, permanent
    tmp = DEVICE_ID_PATH.with_suffix(".tmp")
    tmp.write_text(device_id)
    os.replace(tmp, DEVICE_ID_PATH)  # atomic
    try:
        os.chmod(DEVICE_ID_PATH, 0o600)
    except OSError:
        pass
    return device_id


def default_name(device_id: str) -> str:
    """A friendly default name until the user/controller assigns an alias."""
    return "eximaro-" + device_id[:6]
