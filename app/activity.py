# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
"""A small, human-readable activity log so it's always clear what the device did.

Plain-language events — "Pushed content to 2 displays", "Renamed to Lobby" — are
appended to a capped JSON file in the data dir so an operator can see what the
device has been doing from the health screen, without reading the systemd
journal. This is a friendly activity feed, not a security audit log: it records
what happened, never secrets, passwords, or keys.
"""

import json
import time

from . import config

LOG_PATH = config.DATA / "activity.json"
MAX_ENTRIES = 200  # keep the file tiny; only the most recent events matter


def _load() -> list:
    if LOG_PATH.exists():
        try:
            return json.loads(LOG_PATH.read_text()).get("events", [])
        except (json.JSONDecodeError, OSError):
            pass
    return []


def log(event: str, detail: str = "") -> None:
    """Record one plain-language event. Never raises — writing the activity feed
    must not be able to break the real action that triggered it."""
    try:
        events = _load()
        events.append({
            "ts": time.time(),
            "when": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": str(event),
            "detail": str(detail),
        })
        events = events[-MAX_ENTRIES:]
        config.DATA.mkdir(parents=True, exist_ok=True)
        tmp = LOG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"events": events}, indent=2))
        tmp.replace(LOG_PATH)
    except Exception:
        pass


def recent(n: int = 50) -> list:
    """The last n events, most recent first."""
    return list(reversed(_load()))[:n]
