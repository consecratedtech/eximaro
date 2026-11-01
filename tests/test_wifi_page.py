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
