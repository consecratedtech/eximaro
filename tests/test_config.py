# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Pure-logic tests for app/config.py — role seeding + config persistence.

config.DATA / config.CONFIG_PATH are bound at import time to the temp data dir
(see conftest.py). ``clean_data_dir`` guarantees no leftover config.json so the
EXIMARO_ROLE seed is observable, then wipes it again afterward.
"""

import json

import pytest

from app import config

pytestmark = pytest.mark.usefixtures("clean_data_dir")


# --- seeding from EXIMARO_ROLE ----------------------------------------------

def test_load_config_seeds_role_controller(monkeypatch):
    monkeypatch.setenv("EXIMARO_ROLE", "controller")
    cfg = config.load_config()
    assert cfg["role"] == "controller"


def test_load_config_seeds_role_display(monkeypatch):
    monkeypatch.setenv("EXIMARO_ROLE", "display")
    cfg = config.load_config()
    assert cfg["role"] == "display"


def test_load_config_invalid_role_falls_back_to_none(monkeypatch):
    monkeypatch.setenv("EXIMARO_ROLE", "banana")
    cfg = config.load_config()
    assert cfg["role"] is None


def test_load_config_no_env_role_is_none(monkeypatch):
    monkeypatch.delenv("EXIMARO_ROLE", raising=False)
    cfg = config.load_config()
    assert cfg["role"] is None


def test_load_config_default_shape(monkeypatch):
    monkeypatch.delenv("EXIMARO_ROLE", raising=False)
    cfg = config.load_config()
    assert cfg == {"role": None, "name": None, "sync_hostname": True, "shuffle": False}


# --- save_config -> load_config round-trip ----------------------------------

def test_save_then_load_round_trips(monkeypatch):
    # Ensure the env seed cannot mask what we persisted.
    monkeypatch.delenv("EXIMARO_ROLE", raising=False)
    saved = config.save_config(
        {"role": "display", "name": "Foyer TV", "sync_hostname": False}
    )
    assert saved["role"] == "display"  # save returns the same dict
    loaded = config.load_config()
    assert loaded["role"] == "display"
    assert loaded["name"] == "Foyer TV"
    assert loaded["sync_hostname"] is False


def test_saved_role_overrides_env_seed(monkeypatch):
    monkeypatch.setenv("EXIMARO_ROLE", "controller")
    config.save_config({"role": "display", "name": None, "sync_hostname": True})
    # File is owned by the app once written -> persisted role wins over env.
    assert config.load_config()["role"] == "display"


def test_save_config_writes_valid_json_file():
    config.save_config({"role": "controller", "name": "X", "sync_hostname": True})
    assert config.CONFIG_PATH.exists()
    on_disk = json.loads(config.CONFIG_PATH.read_text())
    assert on_disk["role"] == "controller"
    assert on_disk["name"] == "X"


# --- corrupt / invalid persisted file ---------------------------------------

def test_load_config_persisted_invalid_role_falls_back_to_none(monkeypatch):
    monkeypatch.delenv("EXIMARO_ROLE", raising=False)
    config.CONFIG_PATH.write_text(json.dumps({"role": "nonsense", "name": "Z"}))
    cfg = config.load_config()
    assert cfg["role"] is None
    assert cfg["name"] == "Z"  # other fields still loaded


def test_load_config_corrupt_json_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("EXIMARO_ROLE", "controller")
    config.CONFIG_PATH.write_text("{ this is not json")
    cfg = config.load_config()
    # Unparseable file -> fall back to env defaults, not a crash.
    assert cfg["role"] == "controller"
