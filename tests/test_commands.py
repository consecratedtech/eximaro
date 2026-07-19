# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Pure-logic tests for app/commands.py — HMAC-SHA256 sign/verify."""

import hashlib
import hmac

import pytest

from app import commands

KEY = "ab" * 32          # 64 hex chars == 32-byte key
OTHER_KEY = "cd" * 32


def test_sign_is_deterministic():
    payload = b"hello world"
    assert commands.sign(KEY, payload) == commands.sign(KEY, payload)


def test_sign_matches_reference_hmac():
    payload = b'{"cmd":"play"}'
    expected = hmac.new(bytes.fromhex(KEY), payload, hashlib.sha256).hexdigest()
    assert commands.sign(KEY, payload) == expected


def test_sign_is_64_hex_chars():
    sig = commands.sign(KEY, b"x")
    assert len(sig) == 64
    int(sig, 16)  # parses as hex -> raises if not


def test_verify_round_trip_true():
    payload = b'{"cmd":"reboot","ts":123}'
    sig = commands.sign(KEY, payload)
    assert commands.verify(KEY, payload, sig) is True


def test_verify_false_on_tampered_payload():
    payload = b'{"cmd":"reboot"}'
    sig = commands.sign(KEY, payload)
    tampered = b'{"cmd":"shutdown"}'
    assert commands.verify(KEY, tampered, sig) is False


def test_verify_false_on_wrong_key():
    payload = b"important command"
    sig = commands.sign(KEY, payload)
    assert commands.verify(OTHER_KEY, payload, sig) is False


def test_verify_false_on_malformed_signature():
    payload = b"payload"
    # Not even hex / wrong length: compare_digest must return False, not raise.
    assert commands.verify(KEY, payload, "not-a-valid-signature") is False
    assert commands.verify(KEY, payload, "") is False


def test_verify_false_on_truncated_signature():
    payload = b"payload"
    sig = commands.sign(KEY, payload)
    assert commands.verify(KEY, payload, sig[:-1]) is False


def test_verify_false_on_flipped_char():
    payload = b"payload"
    sig = commands.sign(KEY, payload)
    flipped = ("0" if sig[0] != "0" else "1") + sig[1:]
    assert commands.verify(KEY, payload, flipped) is False


def test_verify_returns_bool_type():
    """compare_digest-based verify should yield a real bool, both ways."""
    payload = b"x"
    sig = commands.sign(KEY, payload)
    assert isinstance(commands.verify(KEY, payload, sig), bool)
    assert isinstance(commands.verify(KEY, payload, "bad"), bool)


def test_empty_payload_round_trips():
    sig = commands.sign(KEY, b"")
    assert commands.verify(KEY, b"", sig) is True


def test_odd_length_key_raises_on_sign():
    """A non-hex / odd-length key cannot be decoded; sign should surface that."""
    with pytest.raises(ValueError):
        commands.sign("abc", b"payload")
