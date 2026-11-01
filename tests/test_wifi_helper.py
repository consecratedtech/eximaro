# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The root helper runs nmcli (so it's exercised on hardware), but its terse-output
parser is pure and easy to get wrong, so we test it directly. The helper has no
.py extension, so we load it from its path."""

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

HELPER = Path(__file__).resolve().parent.parent / "helpers" / "eximaro-wifi"


def _load():
    # The helper has no .py extension, so point a source loader at it explicitly.
    loader = SourceFileLoader("eximaro_wifi_helper", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_split_terse_plain():
    assert _load()._split_terse("Home:80:WPA2") == ["Home", "80", "WPA2"]


def test_split_terse_escaped_colon_in_ssid():
    # nmcli renders an SSID that literally contains ':' as 'My\:Net'.
    assert _load()._split_terse(r"My\:Net:64:WPA2") == ["My:Net", "64", "WPA2"]


def test_split_terse_open_network_has_blank_security():
    assert _load()._split_terse("Cafe:50:") == ["Cafe", "50", ""]


def test_split_terse_trailing_backslash_does_not_crash():
    assert _load()._split_terse("Odd\\") == ["Odd\\"]


# --- what the helper writes back must never carry the Wi-Fi password ----------------

import json  # noqa: E402
import subprocess  # noqa: E402

SECRET = "correct-horse-42"


def _run(tmp_path, monkeypatch, nmcli, action="connect"):
    """Run the helper's main() for one request against a fake nmcli; return the status."""
    mod = _load()
    monkeypatch.setattr(mod, "REQUEST", tmp_path / "wifi.request")
    monkeypatch.setattr(mod, "STATUS", tmp_path / "wifi-status.json")
    monkeypatch.setattr(mod, "_nmcli", nmcli)
    monkeypatch.setattr(mod, "_captive_on", lambda: None)
    monkeypatch.setattr(mod, "_captive_off", lambda: None)
    monkeypatch.setattr(mod, "_set_country", lambda: None)
    mod.REQUEST.write_text(json.dumps({"action": action, "ssid": "Home Net", "password": SECRET}))
    mod.main()
    return mod.STATUS.read_text()


def _ok(*args, **kw):
    return subprocess.CompletedProcess(args, 0, "", "")


def test_a_join_that_times_out_never_records_the_password(tmp_path, monkeypatch):
    # subprocess puts the whole command line - password included - in this error.
    def nmcli(*args, timeout=30):
        if "connect" in args:
            raise subprocess.TimeoutExpired(cmd=["nmcli", *args], timeout=timeout)
        return _ok(*args)
    text = _run(tmp_path, monkeypatch, nmcli)
    assert SECRET not in text
    assert json.loads(text)["error"] == "the network took too long to answer"


def test_an_unexpected_failure_records_only_its_kind(tmp_path, monkeypatch):
    def nmcli(*args, timeout=30):
        if "connect" in args:
            raise RuntimeError(f"broke while using {SECRET}")
        return _ok(*args)
    text = _run(tmp_path, monkeypatch, nmcli)
    assert SECRET not in text
    assert "RuntimeError" in json.loads(text)["error"]


def test_a_failed_join_blanks_the_password_in_nmcli_s_own_words(tmp_path, monkeypatch):
    def nmcli(*args, timeout=30):
        if "connect" in args:
            return subprocess.CompletedProcess(args, 4, "", f"Error: secrets {SECRET} were rejected")
        return _ok(*args)
    st = json.loads(_run(tmp_path, monkeypatch, nmcli))
    assert SECRET not in json.dumps(st)
    assert st["error"] == "could not join Home Net" and "***" in st["detail"]
    assert st["ap_active"] is True           # the setup network comes back for a retry


def test_a_failed_hotspot_blanks_its_password(tmp_path, monkeypatch):
    def nmcli(*args, timeout=30):
        if "hotspot" in args:
            return subprocess.CompletedProcess(args, 1, "", f"Error: bad key {SECRET}")
        return _ok(*args)
    text = _run(tmp_path, monkeypatch, nmcli, action="hotspot")
    assert SECRET not in text


def test_scrub_blanks_before_trimming():
    # A password that straddles the 200-character cut must not survive in part.
    text = "x" * 195 + SECRET
    assert "correct" not in _load()._scrub(text, SECRET)
