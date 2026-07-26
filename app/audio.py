# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
"""Which speaker the device plays sound through.

Sound normally rides HDMI to the TV, which the device picks on its own. Some setups
need to be told instead — a headphone/analogue jack, a soundbar or amplifier, a
USB-C dock, or a machine with two HDMI ports — so the panel offers the choice.

The app is sandboxed and can't write outside its data dir, so it doesn't change the
setting itself: it drops a request file, and a root-owned path unit (installed by
install.sh) applies it and writes back the current state for the panel to render.
Same shape as the update and promote helpers — the request only ever names an audio
output, so it can't become an arbitrary command.
"""

import json

from . import config

REQUEST_PATH = config.DATA / "audio.request"
STATE_PATH = config.DATA / "audio.state"

AUTO = "auto"  # "let the device choose HDMI on its own" — clears any pinned output


def state() -> dict:
    """What the device can play through: {outputs: [{id, name}], current, pinned}.
    Empty when the helper hasn't reported yet (a fresh install, or no sound card)."""
    try:
        data = json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {"outputs": [], "current": "", "pinned": ""}
    return {
        "outputs": data.get("outputs") or [],
        "current": data.get("current") or "",
        "pinned": data.get("pinned") or "",
    }


def request(choice: str) -> None:
    """Ask the helper to switch output. `choice` is a sink id, part of a sink name,
    AUTO to go back to choosing automatically, or "refresh" to just re-read the
    hardware. Newlines are stripped so the request stays a single value."""
    value = " ".join((choice or "").split()).strip()
    if not value:
        value = "refresh"
    try:
        config.DATA.mkdir(parents=True, exist_ok=True)
        REQUEST_PATH.write_text(value + "\n")
    except OSError:
        pass  # a device with no helper installed just keeps its current output


def refresh() -> None:
    """Re-read the available outputs (e.g. a dock was just plugged in)."""
    request("refresh")


def pending() -> bool:
    return REQUEST_PATH.exists()
