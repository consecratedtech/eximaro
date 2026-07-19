# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
"""Pairing: a controller claims a display.

A display shows a short code for a few minutes. The controller sends that code
back with its identity. The code does two jobs: it proves an operator is present
at the display, and it derives a key that protects the controller's signing key
while it crosses the network — so nothing sensitive travels in the clear. Once
claimed, the display trusts commands signed by that controller's key.

Trust is anchored to the permanent Device ID, so a controller can change its
name or address later without breaking the paired link.
"""

import base64
import secrets
import time
import urllib.request
import json as _json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import auth, identity
from .crypto import Vault

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L to avoid misreads
CODE_LENGTH = 8
CODE_TTL = 180  # seconds the code stays valid (matches the on-screen timeout)

# Display-side pending code, kept only in memory (never written to disk).
_pending = {"code": None, "expires": 0.0}


# --- code lifecycle (display side) ------------------------------------------

def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def start_pairing() -> str:
    _pending["code"] = new_code()
    _pending["expires"] = time.time() + CODE_TTL
    return _pending["code"]


def current_code():
    if _pending["code"] and time.time() < _pending["expires"]:
        return _pending["code"]
    _pending["code"] = None
    return None


def cancel_pairing() -> None:
    _pending["code"] = None
    _pending["expires"] = 0.0


# --- code-derived key that protects the site key in transit -----------------

def _key_from_code(code: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000)
    return kdf.derive(code.encode())


def seal_with_code(code: str, plaintext: bytes) -> str:
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_key_from_code(code, salt)).encrypt(nonce, plaintext, None)
    return base64.b64encode(salt + nonce + ciphertext).decode()


def open_with_code(code: str, blob_b64: str) -> bytes:
    blob = base64.b64decode(blob_b64)
    salt, nonce, ciphertext = blob[:16], blob[16:28], blob[28:]
    return AESGCM(_key_from_code(code, salt)).decrypt(nonce, ciphertext, None)


# --- trust records ----------------------------------------------------------

def set_controller(record: dict) -> None:        # display side: its one controller
    Vault().set("controller", record)


def get_controller():
    return Vault().get("controller")


def is_claimed() -> bool:
    return Vault().has("controller")


def forget_controller() -> None:                 # display side: detach from its controller
    Vault().delete("controller")


def list_displays() -> list:                     # controller side: its displays
    return Vault().get("displays", [])


def add_display_record(record: dict) -> None:
    vault = Vault()
    displays = [d for d in vault.get("displays", []) if d["device_id"] != record["device_id"]]
    displays.append(record)
    vault.set("displays", displays)


def remove_display(device_id: str) -> None:
    vault = Vault()
    vault.set("displays", [d for d in vault.get("displays", []) if d["device_id"] != device_id])


# --- the claim (runs ON the display when a controller submits the code) ------

def claim(code: str, controller: dict, sealed_site_key: str) -> dict:
    valid = current_code()
    if not valid or code.strip().upper() != valid:
        raise ValueError("Invalid or expired code.")
    site_key = open_with_code(valid, sealed_site_key).decode()
    set_controller({
        "device_id": controller["device_id"],
        "name": controller.get("name", ""),
        "address": controller.get("address", ""),
        "site_key": site_key,
    })
    cancel_pairing()
    return {"device_id": identity.get_or_create_device_id()}


# --- claiming a display (runs ON the controller) ----------------------------

def claim_display(address: str, port: int, code: str, controller: dict) -> dict:
    """Send our identity + the code-sealed site key to the display, and record
    it once it accepts. Raises on connection/refusal so the UI can report it."""
    sealed = seal_with_code(code.strip().upper(), auth.get_or_create_site_key().encode())
    payload = _json.dumps({
        "code": code.strip().upper(),
        "controller": controller,
        "sealed_site_key": sealed,
    }).encode()
    url = f"http://{address}:{port}/api/pair/claim"
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = _json.loads(resp.read().decode())
    record = {
        "device_id": result["device_id"],
        "name": result.get("name", ""),
        "address": address,
        "port": port,
    }
    add_display_record(record)
    return record
