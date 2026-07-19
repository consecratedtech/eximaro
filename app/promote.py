# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
"""Promoting a display into a controller.

A controller needs LibreOffice + poppler to turn PowerPoint files into images.
The app runs sandboxed (systemd NoNewPrivileges), so it cannot install packages
itself. Instead, when a device is switched to the controller role it drops a
small request file in the data dir; a root-owned systemd path unit (installed by
install.sh) notices the file, installs one fixed set of packages, and writes a
status file the app reads back. The privileged work stays outside the sandbox
and is limited to that one package set — the app can never run arbitrary
commands as root.
"""

import json
import shutil
import time

from . import config

REQUEST_PATH = config.DATA / "promote.request"
STATUS_PATH = config.DATA / "promote.status"


def has_conversion_tools() -> bool:
    """True when LibreOffice and a PDF rasterizer are both present."""
    office = shutil.which("soffice") or shutil.which("libreoffice")
    return bool(office) and bool(shutil.which("pdftoppm"))


def request_promotion() -> None:
    """Ask the privileged helper to install the controller packages. Writing the
    request file is all the (sandboxed) app can do; the helper does the rest."""
    try:
        config.DATA.mkdir(parents=True, exist_ok=True)
        REQUEST_PATH.write_text("requested " + time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
    except OSError:
        pass


def _read_status() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def conversion_state() -> dict:
    """PowerPoint-conversion readiness, for the UI. One of:
    ready | installing | failed | unavailable, with a plain-language detail."""
    if has_conversion_tools():
        return {"state": "ready", "detail": "PowerPoint conversion is available."}
    status = _read_status()
    if REQUEST_PATH.exists() or status.get("state") == "running":
        return {"state": "installing",
                "detail": "Installing PowerPoint support — this takes a few minutes "
                          "and needs an internet connection."}
    if status.get("state") == "failed":
        return {"state": "failed",
                "detail": status.get("detail") or "PowerPoint support could not be installed."}
    return {"state": "unavailable",
            "detail": "PowerPoint conversion isn't installed on this device yet."}
