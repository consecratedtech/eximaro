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
import os

from . import config

REQUEST_PATH = config.DATA / "audio.request"
STATE_PATH = config.DATA / "audio.state"

AUTO = "auto"        # "choose HDMI on its own" — clears any pinned output
REFRESH = "refresh"  # just re-read the hardware


def state() -> dict:
    """What the device can play through: {outputs: [{id, name}], current, pinned}.
    Empty when the helper hasn't reported yet (a fresh install, or no sound card).

    Every field is re-validated rather than trusted: this file is written by a helper
    that may be older than the app (or missing entirely), and a surprise here would
    otherwise take down every page that draws the settings drawer."""
    empty = {"outputs": [], "current": "", "pinned": ""}
    try:
        data = json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    outputs = []
    for entry in data.get("outputs") or []:
        if not isinstance(entry, dict):
            continue
        out_id, name = entry.get("id"), entry.get("name")
        if isinstance(out_id, (str, int)) and isinstance(name, str) and name.strip():
            outputs.append({"id": str(out_id), "name": name.strip()})
    return {
        "outputs": outputs,
        "current": str(data.get("current") or ""),
        "pinned": str(data.get("pinned") or ""),
    }


def installed() -> bool:
    """Whether the privileged helper has ever reported. The app can't switch audio on
    its own, so without this the panel would offer a control that silently does
    nothing — a device updated in-app (which only replaces app code) needs a full
    update to gain the helper."""
    return STATE_PATH.exists()


def request(choice: str) -> None:
    """Ask the helper to switch output. `choice` is a sink name (what the panel
    sends), a sink id, AUTO, or REFRESH.

    Written atomically: the watcher fires on the file appearing, so a plain write
    could be read while still empty and the choice would be silently dropped.
    Collapsing whitespace also keeps the value on the single line the helper reads."""
    value = " ".join((choice or "").split()).strip() or REFRESH
    tmp = REQUEST_PATH.with_suffix(".request.tmp")
    try:
        config.DATA.mkdir(parents=True, exist_ok=True)
        tmp.write_text(value + "\n")
        os.replace(tmp, REQUEST_PATH)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass  # a device with no helper installed just keeps its current output


def request_output(choice: str) -> bool:
    """Switch to one of the outputs the device actually reported (or AUTO). Returns
    False for anything else, so a stale form — or a hand-crafted one — can't pin a
    value the device has no way to honour."""
    value = " ".join((choice or "").split()).strip()
    if value == AUTO:
        request(AUTO)
        return True
    for out in state()["outputs"]:
        if value in (out["id"], out["name"]):
            request(out["name"])   # pin by NAME: ids are re-assigned every boot
            return True
    return False


def refresh() -> None:
    """Re-read the available outputs (e.g. a dock was just plugged in)."""
    request(REFRESH)


def pending() -> bool:
    return REQUEST_PATH.exists()
