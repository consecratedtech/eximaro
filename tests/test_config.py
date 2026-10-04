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


# --- source version stamp ----------------------------------------------------
# install.sh writes a VERSION file beside the app package so a device can answer
# "am I on the current code?" itself, instead of the answer having to be worked
# out by comparing an update timestamp against commit times by hand.

def test_version_stamp_is_read_written_and_degrades_safely():
    import importlib
    from pathlib import Path
    from app import config as cfg

    stamp = Path(cfg.__file__).resolve().parent.parent / "VERSION"
    assert not stamp.exists(), "a stray VERSION file is checked in"
    try:
        # missing (a plain dev checkout) must not raise
        assert cfg._read_version() == "unknown"

        stamp.write_text("8ee2183 installed 2026-08-30T09:20:00-04:00\n")
        importlib.reload(cfg)
        assert cfg.VERSION.startswith("8ee2183")

        # a truncated or empty stamp must degrade, never crash the app on boot
        stamp.write_text("")
        importlib.reload(cfg)
        assert cfg.VERSION == "unknown"
    finally:
        if stamp.exists():
            stamp.unlink()
        importlib.reload(cfg)


# --- reachability: a sign must never show a browser error page ----------------

def test_needs_net_classification_and_caching(monkeypatch):
    from app import net
    net.reset_cache()
    assert net.host_of("https://docs.google.com/presentation/d/e/X/embed") == "docs.google.com"
    assert net.host_of("/asset/abc.png") == ""          # served by this device
    assert net.host_of(None) == ""

    calls = []
    monkeypatch.setattr(net, "_resolves", lambda h: (calls.append(h), False)[1])
    assert net.reachable("docs.google.com") is False
    assert net.reachable("docs.google.com") is False    # cached, not looked up twice
    assert calls == ["docs.google.com"]

    # a purely local playlist must never trigger a lookup at all
    calls.clear()
    assert net.can_reach_all(["/asset/a.png", "/asset/b.mp4"]) is True
    assert calls == []


def test_unreachable_host_makes_a_mixed_playlist_report_false(monkeypatch):
    from app import net
    net.reset_cache()
    monkeypatch.setattr(net, "_resolves", lambda h: False)
    assert net.can_reach_all(["/asset/a.png", "https://docs.google.com/x"]) is False
    net.reset_cache()
    monkeypatch.setattr(net, "_resolves", lambda h: True)
    assert net.can_reach_all(["https://docs.google.com/x"]) is True


def test_cache_expires_so_a_recovered_network_is_noticed(monkeypatch):
    # A screen that went offline must come back on its own when the network
    # returns — a cached "unreachable" verdict can never be trusted forever.
    from app import net
    net.reset_cache()
    monkeypatch.setattr(net, "_resolves", lambda h: True)   # the network is back
    net._cache["h"] = (0.0, False)                          # stale "offline" verdict
    assert net.reachable("h", now=1.0) is False             # still inside the cache window
    assert net.reachable("h", now=net.CACHE_SECONDS + 1) is True   # expired -> re-checked


# --- Google refusing a deck is visible to the server --------------------------

def test_probe_reads_googles_answer(monkeypatch):
    from app import net
    net.reset_cache()
    deck = "https://docs.google.com/presentation/d/e/2PACX-x/embed?start=true"
    monkeypatch.setattr(net, "_fetch", lambda u: (u, 200))
    assert net.probe(deck) == (True, "")
    net.reset_cache()
    monkeypatch.setattr(net, "_fetch", lambda u: (u, 401))
    ok, why = net.probe(deck)
    assert ok is False and "sign in" in why
    net.reset_cache()
    monkeypatch.setattr(net, "_fetch", lambda u: ("https://accounts.google.com/ServiceLogin?x", 200))
    ok, why = net.probe(deck)
    assert ok is False and "sign in" in why
    net.reset_cache()
    def boom(u): raise TimeoutError()
    monkeypatch.setattr(net, "_fetch", boom)
    ok, why = net.probe(deck)
    assert ok is False and "could not reach" in why


def test_probe_is_cached_so_it_cannot_become_a_load_source(monkeypatch):
    from app import net
    net.reset_cache()
    calls = []
    monkeypatch.setattr(net, "_fetch", lambda u: (calls.append(u), (u, 200))[1])
    deck = "https://docs.google.com/presentation/d/e/2PACX-y/embed"
    for _ in range(50):
        net.probe(deck, now=1000.0)
    assert len(calls) == 1
    net.probe(deck, now=1000.0 + net.PROBE_CACHE_SECONDS + 1)
    assert len(calls) == 2


def test_first_problem_explains_the_screen_and_stays_silent_when_local(monkeypatch):
    from app import net
    net.reset_cache()
    monkeypatch.setattr(net, "_resolves", lambda h: True)
    monkeypatch.setattr(net, "_fetch", lambda u: (u, 401))
    assert "sign in" in net.first_problem(["https://docs.google.com/presentation/d/e/2PACX-z/embed"])
    assert net.first_problem(["/asset/a.png"]) == ""
    net.reset_cache()
    monkeypatch.setattr(net, "_resolves", lambda h: False)
    assert net.first_problem(["https://docs.google.com/presentation/d/e/2PACX-z/embed"]) == "no network yet"


# --- retry discipline: try ~2 minutes, then stay quiet 15, then try again ---------

def test_a_failing_host_goes_quiet_after_the_active_window(monkeypatch):
    from app import net
    net.reset_cache()
    calls = []
    monkeypatch.setattr(net, "_resolves", lambda h: (calls.append(h), False)[1])
    t0 = 1000.0
    # keep asking every 20s (the DNS cache length) across the active window
    for s in range(0, net.ACTIVE_WINDOW + 1, net.CACHE_SECONDS):
        net.reachable("deck.example", now=t0 + s)
    active_calls = len(calls)
    assert active_calls >= 2                                   # it did try
    assert net.quiet_for("deck.example", now=t0 + net.ACTIVE_WINDOW + 1) > 0
    # during the quiet period nothing leaves the device, however often the screen asks
    for s in range(1, net.QUIET_PERIOD, 20):
        assert net.reachable("deck.example", now=t0 + net.ACTIVE_WINDOW + s) is False
    assert len(calls) == active_calls
    # after the quiet period it tries again
    net.reachable("deck.example", now=t0 + net.ACTIVE_WINDOW + net.QUIET_PERIOD + 1)
    assert len(calls) == active_calls + 1


def test_a_success_ends_any_quiet_period(monkeypatch):
    from app import net
    net.reset_cache()
    net._quiet["h"] = 5000.0
    monkeypatch.setattr(net, "_resolves", lambda h: True)
    assert net.reachable("h", now=6000.0) is True
    assert net.quiet_for("h", now=6000.0) == 0


def test_google_probe_respects_the_quiet_period_too(monkeypatch):
    from app import net
    net.reset_cache()
    calls = []
    monkeypatch.setattr(net, "_fetch", lambda u: (calls.append(u), (u, 401))[1])
    deck = "https://docs.google.com/presentation/d/e/2PACX-q/embed"
    net.probe(deck, now=0.0)                              # fails -> failure run starts
    net.probe(deck, now=net.PROBE_CACHE_SECONDS + 1)      # second real attempt, still failing
    assert len(calls) == 2
    assert net.quiet_for(deck, now=net.PROBE_CACHE_SECONDS + 2) > 0
    ok, why = net.probe(deck, now=net.PROBE_CACHE_SECONDS + 2)
    assert ok is False and "sign in" in why               # the last reason is still shown
    assert len(calls) == 2                                # and Google was not asked again
