# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
"""Signed commands (HMAC-SHA256).

The controller signs every command to a display with the site key; the display
verifies the signature before acting on it. Anything not correctly signed is
ignored. Because the site key moves with the controller role on hand-off, a new
controller's commands verify without re-pairing the displays.
"""

import hashlib
import hmac


def sign(site_key_hex: str, payload: bytes) -> str:
    return hmac.new(bytes.fromhex(site_key_hex), payload, hashlib.sha256).hexdigest()


def verify(site_key_hex: str, payload: bytes, signature: str) -> bool:
    expected = sign(site_key_hex, payload)
    return hmac.compare_digest(expected, signature)  # constant-time
