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
    import urllib.error
    def never_sent(u): raise urllib.error.URLError(TimeoutError())   # no connection made
    monkeypatch.setattr(net, "_fetch", never_sent)
    ok, why = net.probe(deck)
    assert ok is False and "could not reach" in why
    net.reset_cache()
    def too_slow(u): raise TimeoutError()                           # sent, answer too slow
    monkeypatch.setattr(net, "_fetch", too_slow)
    ok, why = net.probe(deck)
    assert ok is False and "didn't answer in time" in why


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


# --- after a power cut the deck comes back as soon as the network does -----------------

import threading  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402

DECK = "https://docs.google.com/presentation/d/e/2PACX-boot/embed"


def test_with_no_network_address_nothing_is_looked_up_or_counted(monkeypatch):
    from app import net
    net.reset_cache()
    calls = []
    monkeypatch.setattr(net, "_resolves", lambda h: (calls.append(h), False)[1])
    for s in range(0, 600, 3):                       # ten minutes of screen polls, no network
        assert net.first_problem([DECK], now=1000.0 + s, online=False) == "no network yet"
    assert calls == [] and net.quiet_for("docs.google.com", now=1600.0) == 0


def test_the_deck_shows_on_the_first_poll_once_there_is_a_network(monkeypatch):
    # The Pi boots before the router: nothing was counted while it had no address, so
    # the first poll with one checks straight away.
    from app import net
    net.reset_cache()
    monkeypatch.setattr(net, "_resolves", lambda h: True)
    monkeypatch.setattr(net, "_fetch", lambda u: (u, 200))
    for s in range(0, 300, 3):
        net.first_problem([DECK], now=1000.0 + s, online=False)
    assert net.first_problem([DECK], now=1300.0, online=True) == ""


def test_a_flapping_link_still_goes_quiet(monkeypatch):
    # Wi-Fi drops and rejoins every minute while the internet is down: the owner's rule
    # (about two minutes of tries, then fifteen quiet) must still hold.
    from app import net
    net.reset_cache()
    calls = []
    monkeypatch.setattr(net, "_resolves", lambda h: (calls.append(h), False)[1])
    went_quiet = False
    for s in range(0, 3600, 3):
        online = (s // 60) % 2 == 0
        net.first_problem([DECK], now=10000.0 + s, online=online)
        went_quiet = went_quiet or net.quiet_for("docs.google.com", now=10000.0 + s) > 0
    assert went_quiet
    assert len(calls) <= 30                          # a steady network would make about 28


def test_a_page_on_the_device_itself_needs_no_network():
    from app import net
    net.reset_cache()
    for local in ("http://localhost:8080/menu", "http://127.0.0.1/board", "http://[::1]:9000/x"):
        assert net.first_problem([local], online=False) == ""


def test_only_a_request_that_never_left_is_asked_again_soon(monkeypatch):
    from http.client import RemoteDisconnected
    from app import net
    for exc, keep in ((urllib.error.URLError(ConnectionRefusedError()), net.CACHE_SECONDS),
                      (TimeoutError(), net.PROBE_CACHE_SECONDS),
                      (RemoteDisconnected("cut off"), net.PROBE_CACHE_SECONDS)):
        net.reset_cache()
        calls = []

        def fetch(u, exc=exc):
            calls.append(u)
            raise exc
        monkeypatch.setattr(net, "_fetch", fetch)
        net.probe(DECK, now=1000.0)
        net.probe(DECK, now=1000.0 + keep - 1)
        assert len(calls) == 1, exc                  # still the first verdict
        net.probe(DECK, now=1000.0 + keep + 1)
        assert len(calls) == 2, exc


def test_googles_sign_in_redirect_is_read_not_followed():
    # A throttled deck is answered with a redirect to Google's sign-in page. That is the
    # verdict; following it would turn it into an error where accounts.google.com is
    # filtered. A local server stands in for Google.
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from app import net

    class Google(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/throttled"):
                self.send_response(302)
                self.send_header("Location", "https://accounts.google.com/ServiceLogin?continue=x")
            elif self.path.startswith("/moved"):
                self.send_response(302)
                self.send_header("Location", "/deck")
            else:
                self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Google)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        url, status = net._fetch(base + "/throttled")
        assert status == 302 and "accounts.google.com" in url
        assert "sign in" in net._judge(url, status)[1]
        assert net._fetch(base + "/moved") == (base + "/deck", 200)   # other redirects still followed
    finally:
        server.shutdown()


def test_two_polls_at_once_ask_google_once(monkeypatch):
    # A poll that arrives while another is asking waits for that answer instead of
    # starting a second request - or reporting the deck unreachable in the meantime.
    from app import net
    net.reset_cache()
    calls, gate, answers = [], threading.Event(), []

    def slow(u):
        calls.append(u)
        gate.wait(5)
        return u, 200
    monkeypatch.setattr(net, "_fetch", slow)
    first = threading.Thread(target=lambda: answers.append(net.probe(DECK)))
    first.start()
    for _ in range(500):
        if calls:
            break
        time.sleep(0.01)
    second = threading.Thread(target=lambda: answers.append(net.probe(DECK)))
    second.start()
    time.sleep(0.2)
    gate.set()
    first.join()
    second.join()
    assert len(calls) == 1
    assert answers == [(True, ""), (True, "")]


def test_a_poll_during_a_recheck_keeps_the_last_answer(monkeypatch):
    # While a fifteen-minute recheck is running, other polls get the previous verdict
    # straight away, so a deck that is playing is never taken off the screen.
    from app import net
    net.reset_cache()
    gate = threading.Event()
    net._probe_cache[DECK] = (0.0, True, "", net.PROBE_CACHE_SECONDS)       # long expired
    monkeypatch.setattr(net, "_fetch", lambda u: (gate.wait(5), (u, 200))[1])
    first = threading.Thread(target=net.probe, args=(DECK,))
    first.start()
    time.sleep(0.2)
    began = time.monotonic()
    assert net.probe(DECK) == (True, "")
    assert time.monotonic() - began < 1
    gate.set()
    first.join()


def test_opening_the_panel_never_asks_google(monkeypatch):
    from fastapi.testclient import TestClient
    from app import discovery, library, net
    from app.server import create_app
    net.reset_cache()
    monkeypatch.setattr(discovery, "advertise", lambda *a, **k: None)
    fetches = []
    monkeypatch.setattr(net, "_fetch", lambda u: (fetches.append(u), (u, 401))[1])
    cfg = config.load_config()
    cfg["role"] = "controller"
    config.save_config(cfg)
    library.add_url(DECK, 30)
    monkeypatch.setattr(library, "MEASURE_GRACE", -1)   # its length-reading has finished (unread)
    client = TestClient(create_app())
    page = client.get("/content").text
    assert "Google Slides" in page and "Publish to web" in page    # says where to look anyway
    assert fetches == []
    ref = library.list_items()[0]["ref"]              # the link as stored (and as the screen asks)
    net._probe_cache[ref] = (time.time(), False, "Google is asking viewers to sign in for this deck",
                             net.PROBE_CACHE_SECONDS)
    assert "asking viewers to sign in" in client.get("/content").text   # the screen's verdict is shown
    assert fetches == []


def test_screen_data_asks_once_per_poll_and_not_at_all_with_no_address(monkeypatch):
    from fastapi.testclient import TestClient
    from app import discovery, library, net
    from app.server import create_app
    net.reset_cache()
    monkeypatch.setattr(discovery, "advertise", lambda *a, **k: None)
    cfg = config.load_config()
    cfg["role"] = "display"
    config.save_config(cfg)
    library.add_url(DECK, 30)
    resolves, fetches = [], []
    monkeypatch.setattr(net, "_resolves", lambda h: (resolves.append(h), True)[1])
    monkeypatch.setattr(net, "_fetch", lambda u: (fetches.append(u), (u, 200))[1])
    client = TestClient(create_app())
    monkeypatch.setattr(discovery, "has_address", lambda: False)
    data = client.get("/api/screen-data").json()
    assert data["content_reachable"] is False and data["content_note"] == "no network yet"
    assert resolves == [] and fetches == []
    monkeypatch.setattr(discovery, "has_address", lambda: True)
    data = client.get("/api/screen-data").json()
    assert data["content_reachable"] is True and data["content_note"] == ""
    assert len(resolves) == 1 and len(fetches) == 1


def test_has_address_reads_ip_and_never_guesses_offline(monkeypatch):
    import subprocess
    from app import discovery

    def ip(out, code=0):
        return lambda *a, **k: subprocess.CompletedProcess(a, code, out, "")
    monkeypatch.setattr(discovery.subprocess, "run", ip(""))
    assert discovery.has_address() is False                         # nothing yet
    monkeypatch.setattr(discovery.subprocess, "run",
                        ip("3: wlan0    inet6 2001:db8::5/64 scope global dynamic\n"))
    assert discovery.has_address() is True                          # IPv6 counts
    monkeypatch.setattr(discovery.subprocess, "run",
                        ip("1: lo    inet 10.42.0.1/32 scope global lo\n"))
    assert discovery.has_address() is False                         # loopback doesn't
    monkeypatch.setattr(discovery.subprocess, "run", ip("", code=1))
    assert discovery.has_address() is True                          # can't tell -> check

    def missing(*a, **k):
        raise FileNotFoundError("ip")
    monkeypatch.setattr(discovery.subprocess, "run", missing)
    assert discovery.has_address() is True


def test_an_outage_pauses_the_two_minute_clock(monkeypatch):
    # A failure just before the network drops must not make the first failed try after
    # it count as two minutes of trying: the device keeps trying for real first.
    from app import net
    net.reset_cache()
    calls = []
    monkeypatch.setattr(net, "_resolves", lambda h: (calls.append(h), False)[1])
    net.first_problem([DECK], now=1000.0, online=True)                       # one failure
    for s in range(5, 600, 3):
        net.first_problem([DECK], now=1000.0 + s, online=False)               # ten minutes without
    net.first_problem([DECK], now=1600.0, online=True)                       # back, still failing
    assert net.quiet_for("docs.google.com", now=1600.0) == 0                 # still trying
    for s in range(1, 200, 3):
        net.first_problem([DECK], now=1600.0 + s, online=True)
    assert net.quiet_for("docs.google.com", now=1800.0) > 0                  # then goes quiet
    assert 6 <= len(calls) <= 9


def test_has_address_ignores_interfaces_with_no_network_behind_them(monkeypatch):
    import subprocess
    from app import discovery

    def ip(out):
        return lambda *a, **k: subprocess.CompletedProcess(a, 0, out, "")
    for only in ("4: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\n",
                 "5: tailscale0    inet 100.101.102.103/32 scope global tailscale0\n",
                 "3: wlan0    inet 10.42.0.1/24 brd 10.42.0.255 scope global noprefixroute wlan0\n",
                 "2: eth0    inet6 2001:db8::7/64 scope global dadfailed tentative\n"):
        monkeypatch.setattr(discovery.subprocess, "run", ip(only))
        assert discovery.has_address() is False, only
    monkeypatch.setattr(discovery.subprocess, "run",
                        ip("3: wlan0    inet 10.42.0.23/24 brd 10.42.0.255 scope global dynamic wlan0\n"))
    assert discovery.has_address() is True          # a real network that uses 10.42 still counts
    monkeypatch.setattr(discovery.subprocess, "run",
                        ip("2: end0    inet 192.0.2.10/24 brd 192.0.2.255 scope global dynamic end0\n"))
    assert discovery.has_address() is True


def test_a_run_left_over_from_long_ago_starts_again(monkeypatch):
    # A deck taken out of the playlist hours ago, then put back: its old failure must not
    # make the first new failure go straight into the fifteen-minute wait.
    from app import net
    net.reset_cache()
    monkeypatch.setattr(net, "_resolves", lambda h: False)
    net.reachable("docs.google.com", now=1000.0)
    net.reachable("docs.google.com", now=1000.0 + 3 * 3600)
    assert net.quiet_for("docs.google.com", now=1000.0 + 3 * 3600) == 0


# --- a screen with slides set up starts them; the startup screen is only for power-on ---

def test_a_screen_just_switched_on_keeps_checking_until_the_internet_is_up(monkeypatch):
    # After a power cut the router is often back minutes before its internet. A screen
    # that has just been switched on keeps checking on the short cycle, so its slides
    # start as soon as the internet answers; once past starting up, the rule is back.
    from app import net
    net.reset_cache()
    up = {"ok": False}
    calls = []
    monkeypatch.setattr(net, "_resolves", lambda h: (calls.append(h), up["ok"])[1])
    monkeypatch.setattr(net, "_fetch", lambda u: (u, 200))
    for s in range(0, 600, 3):                                  # ten minutes, no internet
        assert net.first_problem([DECK], now=1000.0 + s, starting=True) == "no network yet"
    assert net.quiet_for("docs.google.com", now=1600.0) == 0    # never went quiet
    assert 28 <= len(calls) <= 30                               # one lookup per 20 s
    up["ok"] = True
    assert net.first_problem([DECK], now=1620.0, starting=True) == ""   # slides play

    net.reset_cache()
    up["ok"] = False
    for s in range(0, 600, 3):
        net.first_problem([DECK], now=1000.0 + s, starting=True)
    for s in range(600, 800, 3):                                # past starting up
        net.first_problem([DECK], now=1000.0 + s, starting=False)
    assert net.quiet_for("docs.google.com", now=1800.0) > 0     # two minutes, then quiet


def test_google_turning_a_deck_away_counts_even_while_starting(monkeypatch):
    from app import net
    net.reset_cache()
    monkeypatch.setattr(net, "_resolves", lambda h: True)

    def unreachable(u):
        raise urllib.error.URLError(ConnectionRefusedError())
    monkeypatch.setattr(net, "_fetch", unreachable)
    for s in range(0, 600, 3):                                  # nothing gets to Google
        net.first_problem([DECK], now=1000.0 + s, starting=True)
    assert net.quiet_for(DECK, now=1600.0) == 0
    monkeypatch.setattr(net, "_fetch", lambda u: (u, 401))      # Google says no
    net.first_problem([DECK], now=1620.0, starting=True)
    net.first_problem([DECK], now=1620.0 + net.PROBE_CACHE_SECONDS + 1, starting=True)
    assert net.quiet_for(DECK, now=1620.0 + net.PROBE_CACHE_SECONDS + 2) > 0


def test_the_startup_screen_shows_only_just_after_power_on(monkeypatch):
    from fastapi.testclient import TestClient
    from app import discovery, server
    from app.server import create_app
    monkeypatch.setattr(discovery, "advertise", lambda *a, **k: None)
    on = server._seconds_since_power_on()
    assert on is None or on > 0
    client = TestClient(create_app())
    for on, left in ((35.0, 60), (100.0, 20), (119.0, 1), (200.0, 0), (None, None)):
        monkeypatch.setattr(server, "_seconds_since_power_on", lambda on=on: on)
        assert client.get("/api/screen-data").json()["startup_left"] == left, on


def test_screen_data_tells_the_checks_when_the_screen_was_just_switched_on(monkeypatch):
    from fastapi.testclient import TestClient
    from app import discovery, library, net, server
    from app.server import create_app
    net.reset_cache()
    monkeypatch.setattr(discovery, "advertise", lambda *a, **k: None)
    monkeypatch.setattr(discovery, "has_address", lambda: True)
    cfg = config.load_config()
    cfg["role"] = "display"
    config.save_config(cfg)
    library.add_url(DECK, 30)
    seen = []
    monkeypatch.setattr(net, "first_problem", lambda urls, **kw: (seen.append(kw.get("starting")), "")[1])
    client = TestClient(create_app())
    for on in (60.0, net.STARTUP_GRACE + 1, None):
        monkeypatch.setattr(server, "_seconds_since_power_on", lambda on=on: on)
        client.get("/api/screen-data")
    assert seen == [True, False, False]
