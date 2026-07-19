# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
"""Credentials and the site signing key.

Two different secrets, two different jobs:
  - admin username/password: the *human* gate to the controller UI. The
    password is hashed with Argon2 (never stored or transmitted in plaintext).
  - site key: the *machine* secret used to sign commands to displays. It
    travels with the controller role on hand-off, so a successor controller is
    trusted automatically (displays keep working without re-pairing).
"""

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .crypto import Vault

_ph = PasswordHasher()


def set_credentials(username: str, password: str) -> None:
    Vault().set("admin", {"username": username, "hash": _ph.hash(password)})


def has_credentials() -> bool:
    return Vault().has("admin")


def verify(username: str, password: str) -> bool:
    admin = Vault().get("admin")
    if not admin or admin.get("username") != username:
        return False
    try:
        return _ph.verify(admin["hash"], password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def admin_username():
    """The stored admin username, or None if no password has been set."""
    admin = Vault().get("admin")
    return admin.get("username") if admin else None


def clear_credentials() -> None:
    """Remove the admin password entirely, returning the panel to open access.
    Used by the `reset-password` recovery command and the in-UI 'remove' action."""
    Vault().delete("admin")


def get_or_create_site_key() -> str:
    vault = Vault()
    key = vault.get("site_key")
    if not key:
        key = secrets.token_hex(32)  # 256-bit signing secret
        vault.set("site_key", key)
    return key
