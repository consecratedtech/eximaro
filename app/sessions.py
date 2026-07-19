# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
"""In-memory web sessions for the optional admin login.

A signed-in admin gets an opaque random token in an HttpOnly cookie; the server
keeps the set of live tokens in memory with an expiry. Sessions live only in
memory on purpose: there is exactly one app process, the value is not secret
(it only means "this browser signed in"), and a restart simply asks the operator
to sign in again. Nothing here is written to disk.
"""

import secrets
import time

COOKIE_NAME = "eximaro_session"
TTL = 12 * 3600  # a sign-in lasts 12 hours


_sessions = {}  # token -> expiry (epoch seconds)


def _purge(now: float) -> None:
    for token in [t for t, exp in _sessions.items() if exp <= now]:
        _sessions.pop(token, None)


def create() -> str:
    now = time.time()
    _purge(now)
    token = secrets.token_urlsafe(32)
    _sessions[token] = now + TTL
    return token


def valid(token) -> bool:
    if not token:
        return False
    exp = _sessions.get(token)
    if not exp:
        return False
    if exp <= time.time():
        _sessions.pop(token, None)
        return False
    return True


def destroy(token) -> None:
    if token:
        _sessions.pop(token, None)


def clear_all() -> None:
    """Invalidate every live session (used when the password is removed)."""
    _sessions.clear()
