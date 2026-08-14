# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""app/wifi.py is the app's side of the privileged network helper: it writes JSON
requests for the helper and reads back the results it writes. No nmcli or real
network is involved here — that all lives in the root helper, tested on hardware."""

import json

from app import wifi


def test_request_scan_writes_action(clean_data_dir):
    wifi.request_scan()
    assert wifi.pending() is True
    assert json.loads(wifi.REQUEST_PATH.read_text()) == {"action": "scan"}


def test_request_status_writes_action(clean_data_dir):
    wifi.request_status()
    assert json.loads(wifi.REQUEST_PATH.read_text()) == {"action": "status"}


def test_pending_false_with_no_request(clean_data_dir):
    assert wifi.pending() is False


def test_scan_empty_until_helper_writes(clean_data_dir):
    assert wifi.scan() == []


def test_scan_reads_helper_result(clean_data_dir):
    wifi.SCAN_PATH.write_text(json.dumps(
        {"networks": [{"ssid": "Home", "signal": 80, "secure": True}], "when": "x"}))
    assert wifi.scan() == [{"ssid": "Home", "signal": 80, "secure": True}]


def test_scan_survives_corrupt_file(clean_data_dir):
    wifi.SCAN_PATH.write_text("{not json")
    assert wifi.scan() == []


def test_status_empty_until_helper_writes(clean_data_dir):
    assert wifi.status() == {}


def test_status_reads_helper_result(clean_data_dir):
    wifi.STATUS_PATH.write_text(json.dumps(
        {"wifi": "connected", "ssid": "Home", "ap_active": False}))
    assert wifi.status()["ssid"] == "Home"


# --- ap credentials + join QR -----------------------------------------------

def test_ap_credentials_are_stable(clean_data_dir):
    a = wifi.ap_credentials()
    assert a["ssid"].startswith("Eximaro-Setup-") and len(a["password"]) >= 8
    assert wifi.ap_credentials() == a          # remembered, not regenerated each call


def test_join_qr_payload_matches_creds(clean_data_dir):
    a = wifi.ap_credentials()
    assert wifi.join_qr_payload() == f"WIFI:S:{a['ssid']};T:WPA;P:{a['password']};;"


# --- watcher decision (pure state machine) ----------------------------------
# Signature: _decide(real, ap, configured, manual, down_since, now)

def test_decide_online_no_ap_is_noop():
    assert wifi._decide(True, False, False, False, None, 100) == (None, None)


def test_decide_online_with_ap_stops_setup():
    assert wifi._decide(True, True, False, False, 50, 100) == ("stop", None)


def test_decide_unconfigured_offline_already_hosting_stays():
    assert wifi._decide(False, True, False, False, None, 100) == (None, None)


def test_decide_unconfigured_offline_waits_grace_then_hosts():
    action, ds = wifi._decide(False, False, False, False, None, 100)
    assert action is None and ds == 100                                                  # outage starts counting
    assert wifi._decide(False, False, False, False, 100, 100 + wifi.SETUP_GRACE - 1)[0] is None      # within grace
    assert wifi._decide(False, False, False, False, 100, 100 + wifi.SETUP_GRACE)[0] == "hotspot"     # grace elapsed


# --- the bug: a CONFIGURED device must reconnect, never seize the radio ------

def test_decide_configured_offline_never_hosts_the_setup_ap():
    # Booted before the router / rode out an outage: keep the radio free for the
    # reconnect. Even long past the grace, it must NOT host the setup AP.
    assert wifi._decide(False, False, True, False, 100, 100 + 10_000) == (None, None)


def test_decide_configured_offline_drops_a_stale_setup_ap():
    # If a setup AP is somehow up on a configured device, tear it down so
    # NetworkManager can reclaim the radio and rejoin the saved network.
    assert wifi._decide(False, True, True, False, None, 100) == ("stop", None)


def test_decide_manual_setup_leaves_the_ap_alone_even_when_configured():
    # Operator is reconfiguring by hand — don't fight their setup AP.
    assert wifi._decide(False, True, True, True, None, 100) == (None, None)


def test_decide_online_always_wins_over_manual():
    # A real connection is the goal; drop the AP regardless of the manual flag.
    assert wifi._decide(True, True, True, True, 50, 100) == ("stop", None)


def test_provisioned_flag_roundtrip(clean_data_dir):
    assert not wifi.is_provisioned()
    wifi._mark_provisioned()
    assert wifi.is_provisioned()
    wifi._mark_provisioned()   # idempotent
    assert wifi.is_provisioned()
