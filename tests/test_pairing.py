# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Pure-logic / crypto tests for app/pairing.py.

Covers the pairing code format, the seal/open round-trip and its failure on a
wrong code, and the in-memory code-lifecycle TTL (via monkeypatched time).

Network paths (claim_display) and Vault-backed trust records are out of scope.
"""

import pytest

from cryptography.exceptions import InvalidTag

from app import pairing


# --- code format ------------------------------------------------------------

def test_new_code_length():
    assert len(pairing.new_code()) == pairing.CODE_LENGTH


def test_new_code_uses_only_unambiguous_alphabet():
    alphabet = set(pairing.CODE_ALPHABET)
    for _ in range(200):
        code = pairing.new_code()
        assert set(code) <= alphabet


def test_code_alphabet_excludes_ambiguous_chars():
    # Documented intent: no 0/O/1/I/L to avoid misreads.
    for ch in "01OIL":
        assert ch not in pairing.CODE_ALPHABET


def test_new_code_is_random():
    # Astronomically unlikely to collide if it's actually random.
    codes = {pairing.new_code() for _ in range(50)}
    assert len(codes) > 1


# --- seal/open round-trip ---------------------------------------------------

def test_seal_open_round_trip():
    code = "ABCD2345"
    secret = b"super-secret-site-key-material"
    sealed = pairing.seal_with_code(code, secret)
    assert isinstance(sealed, str)
    assert pairing.open_with_code(code, sealed) == secret


def test_seal_is_nondeterministic_random_salt_nonce():
    code = "ABCD2345"
    secret = b"payload"
    a = pairing.seal_with_code(code, secret)
    b = pairing.seal_with_code(code, secret)
    assert a != b  # fresh salt+nonce each time
    # ...but both still open to the same plaintext.
    assert pairing.open_with_code(code, a) == secret
    assert pairing.open_with_code(code, b) == secret


def test_open_with_wrong_code_raises():
    sealed = pairing.seal_with_code("ABCD2345", b"the site key")
    # A wrong code derives a different key -> AES-GCM auth tag mismatch.
    with pytest.raises(InvalidTag):
        pairing.open_with_code("WRONG234", sealed)


def test_open_with_tampered_blob_raises():
    import base64

    sealed = pairing.seal_with_code("ABCD2345", b"the site key")
    raw = bytearray(base64.b64decode(sealed))
    raw[-1] ^= 0xFF  # flip a ciphertext/tag byte
    tampered = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(InvalidTag):
        pairing.open_with_code("ABCD2345", tampered)


def test_round_trip_empty_payload():
    sealed = pairing.seal_with_code("ABCD2345", b"")
    assert pairing.open_with_code("ABCD2345", sealed) == b""


# --- code lifecycle / TTL (in-memory, time monkeypatched) -------------------

@pytest.fixture(autouse=True)
def _reset_pending():
    """Ensure each lifecycle test starts and ends with no pending code."""
    pairing.cancel_pairing()
    yield
    pairing.cancel_pairing()


def test_start_pairing_returns_current_code(monkeypatch):
    monkeypatch.setattr(pairing.time, "time", lambda: 1000.0)
    code = pairing.start_pairing()
    assert pairing.current_code() == code


def test_current_code_none_before_start():
    assert pairing.current_code() is None


def test_code_expires_after_ttl(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(pairing.time, "time", lambda: now["t"])
    code = pairing.start_pairing()
    assert pairing.current_code() == code
    # Jump just past the TTL window.
    now["t"] = 1000.0 + pairing.CODE_TTL + 1
    assert pairing.current_code() is None


def test_code_valid_just_before_expiry(monkeypatch):
    now = {"t": 5000.0}
    monkeypatch.setattr(pairing.time, "time", lambda: now["t"])
    code = pairing.start_pairing()
    now["t"] = 5000.0 + pairing.CODE_TTL - 1  # still within window
    assert pairing.current_code() == code


def test_cancel_pairing_clears_code(monkeypatch):
    monkeypatch.setattr(pairing.time, "time", lambda: 1000.0)
    pairing.start_pairing()
    pairing.cancel_pairing()
    assert pairing.current_code() is None


# --- controller trust record (display side: set / get / forget) -------------

def test_set_get_forget_controller(clean_data_dir):
    assert pairing.is_claimed() is False
    pairing.set_controller({"device_id": "C1", "name": "Lobby", "address": "1.2.3.4", "site_key": "k"})
    assert pairing.is_claimed() is True
    assert (pairing.get_controller() or {}).get("name") == "Lobby"
    pairing.forget_controller()
    assert pairing.is_claimed() is False
    assert pairing.get_controller() is None
