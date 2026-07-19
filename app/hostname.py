# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
"""Alias -> OS hostname sync.

When a display's alias changes on the controller, the OS hostname follows it —
sanitized to hostname rules, length-capped, and collision-safe — so the network
team sees a friendly name like 'foyer-tv' in the DHCP/router table instead of a
random one. Pairing is unaffected: trust keys off the immutable Device ID, not
the hostname, so a rename never breaks a paired link.

On by default; can be turned off (config 'sync_hostname') for strict networks
that don't want devices renaming their own hostnames.
"""

import os
import re
import shutil
import subprocess

HOSTNAME_MAX = 63


def slugify(alias: str) -> str:
    """'Foyer TV' -> 'foyer-tv'. Always returns a valid, non-empty label."""
    s = alias.strip().lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)   # spaces/symbols -> dash
    s = re.sub(r"-{2,}", "-", s).strip("-")
    s = s[:HOSTNAME_MAX].strip("-")
    return s or "eximaro"


def unique_label(alias: str, device_id: str, taken=()) -> str:
    """Controller-side: keep hostnames unique across the fleet. If the slug is
    already taken, append a short, stable suffix from the Device ID."""
    base = slugify(alias)
    if base not in set(taken):
        return base
    suffix = "-" + device_id[:2]
    return slugify(base[:HOSTNAME_MAX - len(suffix)] + suffix)


def apply_hostname(label: str) -> tuple[bool, str]:
    """Write the OS hostname. No-ops gracefully (returns False + reason) when
    not root or hostnamectl is unavailable, so it never crashes the app."""
    label = slugify(label)
    if os.geteuid() != 0:
        return False, "not root"
    tool = shutil.which("hostnamectl")
    if not tool:
        return False, "hostnamectl not found"
    try:
        subprocess.run(
            [tool, "set-hostname", label],
            check=True, capture_output=True, timeout=10,
        )
        return True, label
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return False, f"failed: {exc}"
