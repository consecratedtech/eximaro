# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
"""Encrypted-at-rest secret vault (AES-256-GCM).

All sensitive material — the admin password hash, the site signing key, and
pairing trust records — lives here, never plaintext on disk. The encryption key
is a random 32-byte master key at DATA/master.key (0600). Binding that master
key to a TPM / secure element is a future hardening step.
"""

import json
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config

MASTER_KEY_PATH = config.DATA / "master.key"
VAULT_PATH = config.DATA / "secrets.enc"


def _load_master_key() -> bytes:
    if MASTER_KEY_PATH.exists():
        key = MASTER_KEY_PATH.read_bytes()
        if len(key) == 32:
            return key
    config.DATA.mkdir(parents=True, exist_ok=True)
    key = AESGCM.generate_key(bit_length=256)
    tmp = MASTER_KEY_PATH.with_suffix(".tmp")
    tmp.write_bytes(key)
    os.replace(tmp, MASTER_KEY_PATH)
    try:
        os.chmod(MASTER_KEY_PATH, 0o600)
    except OSError:
        pass
    return key


class Vault:
    """A tiny encrypted key/value store. Low write volume, so load-modify-save
    of one encrypted document is plenty and keeps it simple and atomic."""

    def __init__(self) -> None:
        self._key = _load_master_key()

    def _read(self) -> dict:
        if not VAULT_PATH.exists():
            return {}
        blob = VAULT_PATH.read_bytes()
        nonce, ciphertext = blob[:12], blob[12:]
        raw = AESGCM(self._key).decrypt(nonce, ciphertext, None)
        return json.loads(raw.decode())

    def _write(self, data: dict) -> None:
        config.DATA.mkdir(parents=True, exist_ok=True)
        nonce = secrets.token_bytes(12)  # fresh nonce every write
        ciphertext = AESGCM(self._key).encrypt(nonce, json.dumps(data).encode(), None)
        tmp = VAULT_PATH.with_suffix(".tmp")
        tmp.write_bytes(nonce + ciphertext)
        os.replace(tmp, VAULT_PATH)
        try:
            os.chmod(VAULT_PATH, 0o600)
        except OSError:
            pass

    def get(self, key: str, default=None):
        return self._read().get(key, default)

    def set(self, key: str, value) -> None:
        data = self._read()
        data[key] = value
        self._write(data)

    def has(self, key: str) -> bool:
        return key in self._read()

    def delete(self, key: str) -> None:
        data = self._read()
        data.pop(key, None)
        self._write(data)
