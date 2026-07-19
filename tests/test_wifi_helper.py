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
