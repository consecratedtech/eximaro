# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""labeled_ips(): turn `ip -o -4 addr` output into reachable addresses, Wi-Fi
first, with loopback / link-local / virtual interfaces dropped. Nothing here
touches the real network — the command output and the wireless check are faked."""

import subprocess
import types

from app import discovery


def _fake_ip(lines):
    output = "\n".join(lines)

    def runner(cmd, **kwargs):   # absorb capture_output/text/timeout without shadowing
        return types.SimpleNamespace(stdout=output, returncode=0)

    return runner


# A realistic `ip -o -4 addr show` for a box with both wired and Wi-Fi.
DUAL = [
    "1: lo    inet 127.0.0.1/8 scope host lo",
    "2: eth0    inet 192.168.1.10/24 brd 192.168.1.255 scope global eth0",
    "3: wlan0    inet 192.168.1.20/24 brd 192.168.1.255 scope global wlan0",
]


def test_labeled_ips_wifi_first_and_labeled(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_ip(DUAL))
    monkeypatch.setattr(discovery.os.path, "isdir", lambda p: p.endswith("/wlan0/wireless"))
    out = discovery.labeled_ips()
    assert [e["ip"] for e in out] == ["192.168.1.20", "192.168.1.10"]   # Wi-Fi leads
    assert [e["label"] for e in out] == ["Wi-Fi", "Ethernet"]


def test_labeled_ips_drops_loopback_linklocal_and_virtual(monkeypatch):
    lines = [
        "1: lo    inet 127.0.0.1/8 scope host lo",
        "2: eth0    inet 169.254.5.5/16 scope link eth0",         # link-local: skip
        "3: docker0    inet 172.17.0.1/16 scope global docker0",  # virtual: skip
        "4: eth0    inet 10.0.0.5/24 scope global eth0",          # keep
    ]
    monkeypatch.setattr(subprocess, "run", _fake_ip(lines))
    monkeypatch.setattr(discovery.os.path, "isdir", lambda p: False)
    out = discovery.labeled_ips()
    assert [e["ip"] for e in out] == ["10.0.0.5"]
    assert out[0]["label"] == "Ethernet"


def test_labeled_ips_empty_when_no_real_address(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_ip(["1: lo    inet 127.0.0.1/8 scope host lo"]))
    monkeypatch.setattr(discovery.os.path, "isdir", lambda p: False)
    assert discovery.labeled_ips() == []


def test_lan_ips_follows_labeled_order(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_ip(DUAL))
    monkeypatch.setattr(discovery.os.path, "isdir", lambda p: p.endswith("/wlan0/wireless"))
    assert discovery.lan_ips() == ["192.168.1.20", "192.168.1.10"]


# --- setup-AP detection (10.42.x means we're hosting, not connected) ---------

def test_hosting_ap_true_and_no_real_ips_when_only_ap(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_ip(
        ["2: wlan0    inet 10.42.0.1/24 scope global wlan0"]))
    monkeypatch.setattr(discovery.os.path, "isdir", lambda p: True)
    assert discovery.hosting_ap() is True
    assert discovery.real_ips() == []          # the AP address isn't a real connection


def test_real_ips_excludes_the_ap_address(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_ip([
        "2: eth0    inet 192.168.1.10/24 scope global eth0",
        "3: wlan0    inet 10.42.0.1/24 scope global wlan0",
    ]))
    monkeypatch.setattr(discovery.os.path, "isdir", lambda p: p.endswith("/wlan0/wireless"))
    assert [e["ip"] for e in discovery.real_ips()] == ["192.168.1.10"]
    assert discovery.hosting_ap() is True
