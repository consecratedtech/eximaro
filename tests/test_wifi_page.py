# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The Wi-Fi setup page: what it shows, and who can use it."""

import json

import pytest
from fastapi.testclient import TestClient

from app import config, discovery, wifi

pytestmark = pytest.mark.usefixtures("clean_data_dir")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(discovery, "advertise", lambda *a, **k: None)


def _client(**cfg_extra):
    from app.server import create_app
    cfg = config.load_config()
    cfg.update(cfg_extra)
    config.save_config(cfg)
    return TestClient(create_app())


def test_the_wifi_page_never_prints_the_helpers_error_text():
    # What an older helper wrote after a slow join: the whole command, password and all.
    wifi.STATUS_PATH.write_text(json.dumps({
        "error": "Command '['/usr/bin/nmcli', 'device', 'wifi', 'connect', 'Home', "
                 "'password', 'correct-horse-42']' timed out after 45 seconds",
        "action": "connect"}))
    page = _client(role="display").get("/wifi-setup").text
    assert "correct-horse-42" not in page and "nmcli" not in page
    assert "Check its name and password" in page


# --- who can use it -----------------------------------------------------------------

LAN = ("192.0.2.50", 50000)       # someone on the building's normal network
PHONE = ("10.42.0.7", 50000)      # a phone that joined this screen's setup network
TV = ("127.0.0.1", 50000)         # the screen's own browser
SETUP_HOST = "http://10.42.0.1:8080"


def _locked(monkeypatch, hosting):
    from app import auth
    from app.server import create_app
    monkeypatch.setattr(discovery, "hosting_ap", lambda: hosting)
    auth.set_credentials("admin", "secret12")
    _client(role="display")
    return create_app()


@pytest.mark.parametrize("method,path", [
    ("get", "/wifi-setup"), ("get", "/wifi-qr.png"), ("get", "/wifi-open-qr.png"),
    ("post", "/api/wifi/connect"), ("post", "/api/wifi/stop"), ("post", "/api/wifi/start"),
])
def test_from_the_normal_network_wifi_setup_needs_signing_in(monkeypatch, method, path):
    client = TestClient(_locked(monkeypatch, hosting=False), client=LAN)
    if method == "post":
        r = client.post(path, data={"ssid": "Elsewhere", "password": "x"}, follow_redirects=False)
    else:
        r = client.get(path, follow_redirects=False)
    assert r.status_code in (303, 401)
    if r.status_code == 303:
        assert r.headers["location"] == "/login"
    assert not wifi.pending()                      # nothing was asked of the network helper


def test_a_phone_on_the_setup_network_can_finish_setup_without_signing_in(monkeypatch):
    client = TestClient(_locked(monkeypatch, hosting=True), client=PHONE, base_url=SETUP_HOST)
    assert client.get("/wifi-setup").status_code == 200
    r = client.post("/api/wifi/connect", data={"ssid": "Home", "password": "pw123456"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/wifi-setup"
    assert json.loads(wifi.REQUEST_PATH.read_text())["ssid"] == "Home"


def test_a_setup_network_address_counts_only_while_this_screen_hosts_it(monkeypatch):
    client = TestClient(_locked(monkeypatch, hosting=False), client=PHONE, base_url=SETUP_HOST)
    r = client.get("/wifi-setup", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_the_setup_network_does_not_open_anything_else(monkeypatch):
    client = TestClient(_locked(monkeypatch, hosting=True), client=PHONE, base_url=SETUP_HOST)
    assert client.post("/api/wifi/start", follow_redirects=False).status_code == 401
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_the_tv_can_always_show_its_setup_codes(monkeypatch):
    client = TestClient(_locked(monkeypatch, hosting=False), client=TV)
    for path in ("/wifi-qr.png", "/wifi-open-qr.png"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (200, 404)         # 404 only where QR rendering isn't installed
    assert client.get("/wifi-setup", follow_redirects=False).status_code == 303


def test_with_no_password_nothing_changes(monkeypatch):
    from app.server import create_app
    monkeypatch.setattr(discovery, "hosting_ap", lambda: False)
    _client(role="display")
    client = TestClient(create_app(), client=LAN)
    assert client.get("/wifi-setup").status_code == 200


# --- moving a screen to another network from the normal network ----------------------

WIFI_NOW = [{"ip": "192.0.2.10", "iface": "wlan0", "wifi": True, "label": "Wi-Fi"}]
WIRED = [{"ip": "192.0.2.10", "iface": "eth0", "wifi": False, "label": "Ethernet"}]


def _on(monkeypatch, addrs, ssid="Home Net"):
    monkeypatch.setattr(discovery, "labeled_ips", lambda: addrs)
    monkeypatch.setattr(discovery, "hosting_ap", lambda: False)
    wifi.STATUS_PATH.write_text(json.dumps({"wifi": "connected", "ssid": ssid}))


def test_moving_a_wifi_only_screen_needs_the_screen_first(monkeypatch):
    _on(monkeypatch, WIFI_NOW)
    client = TestClient(_client(role="display", name="Lobby").app, client=LAN)
    r = client.post("/api/wifi/connect", data={"ssid": "Office", "password": "pw123456"},
                    follow_redirects=False)
    assert r.status_code == 200 and not wifi.pending()           # nothing asked of the helper yet
    page = r.text
    assert "Move Lobby to Office?" in page
    assert "Lobby leaves <b>Home Net</b> right away" in page and "network cable brings it back" in page
    assert "Keep it on Home Net" in page
    assert page.index('<form method="get" action="/">') < page.index('name="confirm" value="yes"')
    assert r.headers["cache-control"] == "no-store"              # the password is in the yes form


def test_the_explicit_yes_moves_it(monkeypatch):
    _on(monkeypatch, WIFI_NOW)
    client = TestClient(_client(role="display").app, client=LAN)
    r = client.post("/api/wifi/connect", data={"ssid": "Office", "password": "pw123456", "confirm": "yes"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/wifi-setup"
    assert json.loads(wifi.REQUEST_PATH.read_text()) == {"action": "connect", "ssid": "Office",
                                                         "password": "pw123456"}


def test_a_wired_screen_gets_the_gentle_version(monkeypatch):
    _on(monkeypatch, WIRED)
    page = TestClient(_client(role="display", name="Lobby").app, client=LAN).post(
        "/api/wifi/connect", data={"ssid": "Office", "password": "x"}).text
    assert "Connect Lobby to Office?" in page and "stays on its network cable" in page
    assert 'class="danger"' not in page


def test_the_move_screen_names_who_loses_touch(monkeypatch):
    from app import pairing
    _on(monkeypatch, WIFI_NOW)
    pairing.set_controller({"device_id": "c0ffee00", "name": "Sanctuary", "address": "192.0.2.5",
                            "site_key": "ab" * 32})
    page = TestClient(_client(role="display").app, client=LAN).post(
        "/api/wifi/connect", data={"ssid": "Office"}).text
    assert "Sanctuary can only send it content while they&rsquo;re on the same network" in page
    pairing.forget_controller()
    pairing.add_display_record({"device_id": "aaaa1111", "name": "Lobby", "address": "192.0.2.8", "port": 8080})
    page = TestClient(_client(role="controller").app, client=LAN).post(
        "/api/wifi/connect", data={"ssid": "Office"}).text
    assert "Lobby only gets content while on the same network as this controller" in page


def test_the_move_screen_shows_names_as_text(monkeypatch):
    _on(monkeypatch, WIFI_NOW, ssid="<i>Home</i>")
    page = TestClient(_client(role="display").app, client=LAN).post(
        "/api/wifi/connect", data={"ssid": '"><b>x</b>', "password": '"><b>pw</b>'}).text
    assert "<b>x</b>" not in page and "<i>Home</i>" not in page and "<b>pw</b>" not in page
    assert 'name="ssid" value="&quot;&gt;&lt;b&gt;x&lt;/b&gt;"' in page


def test_a_blank_name_asks_nothing(monkeypatch):
    _on(monkeypatch, WIFI_NOW)
    r = TestClient(_client(role="display").app, client=LAN).post(
        "/api/wifi/connect", data={"ssid": "   "}, follow_redirects=False)
    assert r.status_code == 303 and not wifi.pending()


# --- on the setup network ------------------------------------------------------------

def test_the_setup_network_page_says_what_connect_does_and_has_no_off_switch(monkeypatch):
    monkeypatch.setattr(discovery, "hosting_ap", lambda: True)
    wifi.STATUS_PATH.write_text(json.dumps({"ap_active": True}))
    page = TestClient(_client(role="display").app, client=PHONE, base_url=SETUP_HOST).get("/wifi-setup").text
    assert "your phone drops off it" in page
    assert "Stop setup network" not in page


def test_from_the_normal_network_the_off_switch_is_offered(monkeypatch):
    monkeypatch.setattr(discovery, "hosting_ap", lambda: True)
    wifi.STATUS_PATH.write_text(json.dumps({"ap_active": True}))
    page = TestClient(_client(role="display").app, client=TV).get("/wifi-setup").text
    assert "Stop setup network" in page and "your phone drops off it" not in page


def test_a_phone_on_the_setup_network_cannot_turn_it_off_when_locked(monkeypatch):
    client = TestClient(_locked(monkeypatch, hosting=True), client=PHONE, base_url=SETUP_HOST)
    assert client.post("/api/wifi/stop", follow_redirects=False).status_code == 401


# --- review follow-ups -----------------------------------------------------------------

def test_a_real_network_in_the_same_range_is_not_the_setup_network(monkeypatch):
    # A LAN that hands out 10.42.0.x (a laptop sharing its connection does) is not this
    # screen's setup network: the request didn't arrive at the setup gateway address.
    client = TestClient(_locked(monkeypatch, hosting=True), client=("10.42.0.57", 50000),
                        base_url="http://lobby.local:8080")
    r = client.get("/wifi-setup", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.post("/api/wifi/connect", data={"ssid": "Elsewhere"},
                       follow_redirects=False).status_code == 401
    assert not wifi.pending()


BOTH = [{"ip": "192.0.2.10", "iface": "wlan0", "wifi": True, "label": "Wi-Fi"},
        {"ip": "192.0.2.11", "iface": "eth0", "wifi": False, "label": "Ethernet"}]


def test_a_screen_on_cable_and_wifi_is_told_the_truth(monkeypatch):
    _on(monkeypatch, BOTH)
    _client(role="display", name="Lobby")
    from app.server import create_app
    app = create_app()
    over_wifi = TestClient(app, client=LAN, base_url="http://192.0.2.10:8080").post(
        "/api/wifi/connect", data={"ssid": "Office"}).text
    assert "Move Lobby to Office?" in over_wifi and 'class="danger"' in over_wifi
    assert "this page stops answering here" in over_wifi
    assert 'reachable at <a href="http://192.0.2.11:8080">' in over_wifi
    assert "nothing it&rsquo;s doing now stops" not in over_wifi
    over_cable = TestClient(app, client=LAN, base_url="http://192.0.2.11:8080").post(
        "/api/wifi/connect", data={"ssid": "Office"}).text
    assert "This page stays open: it reached the screen over its network cable." in over_cable


def test_the_setup_page_promises_only_what_holds(monkeypatch):
    monkeypatch.setattr(discovery, "hosting_ap", lambda: True)
    page = TestClient(_client(role="display").app, client=PHONE, base_url=SETUP_HOST).get("/wifi-setup").text
    assert "within a minute" not in page
    assert "normally brings the setup network back" in page and "network cable" in page
