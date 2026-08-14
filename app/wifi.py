# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
"""WiFi setup — talk to the privileged network helper.

The app is sandboxed and can't run nmcli itself, so to do anything with the network
it drops a small JSON request in the data dir. A root-owned path unit (installed by
install.sh) runs the helper, which performs the one action and writes the result
back to a file here. The app only ever writes a request and reads a result — it
holds no network privilege of its own.
"""

import json
import secrets
import threading
import time

from . import config, discovery, identity

REQUEST_PATH = config.DATA / "wifi.request"
SCAN_PATH = config.DATA / "wifi-scan.json"
STATUS_PATH = config.DATA / "wifi-status.json"
AP_PATH = config.DATA / "wifi-ap.json"

# No 0/O/1/I/L so the password is easy to read off a screen, and no characters the
# Wi-Fi QR format treats specially, so the join code needs no escaping.
_AP_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789abcdefghjkmnpqrstuvwxyz"


def _request(payload: dict) -> None:
    """Drop a request for the helper. Written atomically so the watcher never sees
    a half-written file."""
    try:
        config.DATA.mkdir(parents=True, exist_ok=True)
        tmp = REQUEST_PATH.with_suffix(".request.tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(REQUEST_PATH)
    except OSError:
        pass


def ap_credentials() -> dict:
    """The setup network's name + password, made once and remembered, so the QR on
    screen always matches the network the Pi is actually hosting."""
    try:
        return json.loads(AP_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    creds = {
        "ssid": "Eximaro-Setup-" + identity.get_or_create_device_id()[:4].upper(),
        "password": "".join(secrets.choice(_AP_ALPHABET) for _ in range(10)),
    }
    try:
        config.DATA.mkdir(parents=True, exist_ok=True)
        AP_PATH.write_text(json.dumps(creds))
    except OSError:
        pass
    return creds


def join_qr_payload() -> str:
    """The 'join this Wi-Fi' string a phone camera understands. Scanning it connects
    the phone to the setup network so it can open the setup page. Our SSID/password
    use only QR-safe characters, so no escaping is needed."""
    creds = ap_credentials()
    return f"WIFI:S:{creds['ssid']};T:WPA;P:{creds['password']};;"


# NetworkManager's shared (hotspot) mode always puts the Pi at this gateway address
# on the setup network, so the setup page is reachable there once a phone has joined.
AP_GATEWAY = "10.42.0.1"


def setup_page_url() -> str:
    """The WiFi-setup page's address on the hosted setup network — a fixed, known
    URL (never client input) that a phone can scan to open directly, instead of
    typing it after joining the setup network."""
    return f"http://{AP_GATEWAY}:{config.PORT}/wifi-setup"


def request_hotspot() -> dict:
    """Ask the helper to host the setup network. Returns the creds so the caller can
    show them and build the QR."""
    creds = ap_credentials()
    _request({"action": "hotspot", "ssid": creds["ssid"], "password": creds["password"]})
    return creds


def request_connect(ssid: str, password: str = "") -> None:
    """Ask the helper to join a real network (which drops the setup AP)."""
    _request({"action": "connect", "ssid": ssid, "password": password})


def request_stop() -> None:
    """Ask the helper to take the setup network down."""
    _request({"action": "stop"})


def request_scan() -> None:
    """Ask the helper to list nearby networks; the result lands in wifi-scan.json."""
    _request({"action": "scan"})


def request_status() -> None:
    """Ask the helper to refresh what the radio is doing (wifi-status.json)."""
    _request({"action": "status"})


def scan() -> list:
    """Nearby networks the helper last found — [{ssid, signal, secure}], strongest
    first. Empty until a scan has run (or if the helper isn't installed)."""
    try:
        return json.loads(SCAN_PATH.read_text()).get("networks", [])
    except (json.JSONDecodeError, OSError):
        return []


def status() -> dict:
    """The helper's last network status: {wifi, ssid, ap_active, when}, or {}."""
    try:
        return json.loads(STATUS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def pending() -> bool:
    """True while a request is still waiting for the helper to pick it up."""
    return REQUEST_PATH.exists()


# --- autonomous setup mode --------------------------------------------------
# A background watcher drives the flow: a device that has NEVER been configured hosts
# the setup network when offline (so the screen shows the join QR); a device that
# already has a network must instead keep RECONNECTING to it — hosting the setup AP
# would seize the one radio and block the reconnect, stranding a sign that merely
# booted before its router or rode out a brief outage. Detection is app-side; only
# host/stop go through the helper.

SETUP_GRACE = 45  # seconds offline before an UNCONFIGURED device offers setup

PROVISIONED_PATH = config.DATA / "wifi-provisioned"  # set once the device is ever online
MANUAL_SETUP_WINDOW = 600  # seconds a hand-started setup AP is left alone by the watcher

_manual_setup_until = 0.0  # monotonic deadline; module-level, shared with the watcher thread


def _mark_provisioned() -> None:
    """Remember that this device has had a working network — from then on an outage
    means 'reconnect', not 'offer setup'."""
    try:
        config.DATA.mkdir(parents=True, exist_ok=True)
        if not PROVISIONED_PATH.exists():
            PROVISIONED_PATH.write_text("1")
    except OSError:
        pass


def is_provisioned() -> bool:
    return PROVISIONED_PATH.exists()


def start_manual_setup() -> None:
    """Operator explicitly asked to (re)configure WiFi from the panel. Host the setup
    AP and tell the watcher to leave it up for a while — otherwise, on a device that
    already has a saved network, the watcher would immediately tear it back down."""
    global _manual_setup_until
    _manual_setup_until = time.monotonic() + MANUAL_SETUP_WINDOW
    request_hotspot()


def _decide(real, ap, configured, manual, down_since, now, grace=SETUP_GRACE):
    """Pure decision for the watcher (split out so it can be tested without a radio).
    Returns (action, down_since') where action is None, 'hotspot' (start setup), or
    'stop' (drop the setup AP).

      real       - a genuine, non-setup connection is up right now
      configured - a network is already saved / the device has been online before
      manual     - the operator is running setup by hand right now (leave the AP alone)
    """
    if real:
        return ("stop" if ap else None), None      # online: drop any setup AP
    if manual:
        return None, None                          # hand-run setup: don't touch it
    if configured:
        # A network is saved. Never seize the radio for a setup AP — that blocks the
        # reconnect and deadlocks. Free the radio (drop a stale AP) and let
        # NetworkManager keep retrying the saved network until it comes back.
        return ("stop" if ap else None), None
    # Never configured and offline: this is genuine first-time setup.
    if ap:
        return None, None                          # already hosting; leave it up
    if down_since is None:
        return None, now                           # start counting the outage
    if now - down_since >= grace:
        return "hotspot", down_since
    return None, down_since


def _watch_loop():
    down_since = None
    ticks = 0
    request_status()   # learn straight away whether a network is already saved
    while True:
        time.sleep(5)
        ticks += 1
        try:
            real = bool(discovery.real_ips())
            if real:
                _mark_provisioned()
            # Keep the saved-network signal fresh, especially while offline, so a boot
            # before the router doesn't misread it as an unconfigured device.
            if not real and ticks % 6 == 1:        # ~every 30s while offline
                request_status()
            configured = is_provisioned() or bool(status().get("has_saved"))
            action, down_since = _decide(
                real, discovery.hosting_ap(), configured,
                time.monotonic() < _manual_setup_until,
                down_since, time.monotonic())
            if action == "stop":
                request_stop()
            elif action == "hotspot" and not pending():
                request_hotspot()
        except Exception:
            pass


def start_watch() -> None:
    """Start the background setup-mode watcher. Called once from the entry point so
    it runs on the device but never during tests."""
    threading.Thread(target=_watch_loop, name="wifi-watch", daemon=True).start()
