# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The pairing code is no longer sent with the key.

The controller used to send the code a screen showed right next to the site key it
had sealed with that code, so anyone who saw that one request - or a device posing as
the screen under its name - could open the seal and take the key every screen of the
site trusts. Now only the sealed key is sent; the screen shows it knows the code by
opening the seal.
"""

import io
import json
import urllib.error

import pytest

from app import auth, pairing

pytestmark = pytest.mark.usefixtures("clean_data_dir")

CONTROLLER = {"device_id": "c0ffee00", "name": "eximaro-c0ffee", "address": "192.0.2.5"}


@pytest.fixture(autouse=True)
def _no_pending_code():
    pairing.cancel_pairing()
    yield
    pairing.cancel_pairing()


class _Reply:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_controller_never_sends_the_code(monkeypatch):
    seen = {}
    def urlopen(req, timeout=10):
        seen["body"] = req.data
        return _Reply({"device_id": "aaaa1111", "name": "Lobby"})
    monkeypatch.setattr(pairing.urllib.request, "urlopen", urlopen)
    pairing.claim_display("192.0.2.8", 8080, "abcd2345", dict(CONTROLLER))
    body = json.loads(seen["body"])
    assert "code" not in body
    assert b"ABCD2345" not in seen["body"] and b"abcd2345" not in seen["body"]
    # ...and what is sent opens with the code, and only with it.
    key = pairing.open_with_code("ABCD2345", body["sealed_site_key"]).decode()
    assert key == auth.get_or_create_site_key()


def test_a_screen_takes_a_key_sealed_with_its_code():
    code = pairing.start_pairing()
    sealed = pairing.seal_with_code(code, b"ab" * 32)
    pairing.claim(dict(CONTROLLER), sealed)
    assert pairing.get_controller()["site_key"] == "ab" * 32
    assert pairing.current_code() is None                 # used up


def test_a_device_that_tries_wrong_codes_is_stopped_but_nobody_else_is():
    code = pairing.start_pairing()
    wrong = "ABCDEFGH" if code != "ABCDEFGH" else "HGFEDCBA"
    for _ in range(pairing.CODE_TRIES):
        with pytest.raises(ValueError):
            pairing.claim(dict(CONTROLLER), pairing.seal_with_code(wrong, b"k"), client="192.0.2.66")
    # That device isn't listened to again for this code, even with the right one...
    with pytest.raises(ValueError):
        pairing.claim(dict(CONTROLLER), pairing.seal_with_code(code, b"k"), client="192.0.2.66")
    assert not pairing.is_claimed()
    # ...but the code is still on the screen, and the real controller pairs with it.
    assert pairing.current_code() == code
    pairing.claim(dict(CONTROLLER), pairing.seal_with_code(code, b"ab" * 32), client="192.0.2.5")
    assert pairing.get_controller()["site_key"] == "ab" * 32


def test_garbage_in_place_of_a_sealed_key_is_a_wrong_try():
    pairing.start_pairing()
    with pytest.raises(ValueError):
        pairing.claim(dict(CONTROLLER), "not base64 at all!")
    assert not pairing.is_claimed()


def test_a_controller_from_before_still_pairs_and_its_code_must_match():
    code = pairing.start_pairing()
    sealed = pairing.seal_with_code(code, b"ab" * 32)
    with pytest.raises(ValueError):
        pairing.claim(dict(CONTROLLER), sealed, code="WRONG234")
    pairing.claim(dict(CONTROLLER), sealed, code=code.lower())
    assert pairing.is_claimed()


def test_pairing_a_screen_from_before_says_to_update_it(monkeypatch):
    def urlopen(req, timeout=10):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                     io.BytesIO(b'{"error": "\'code\'"}'))
    monkeypatch.setattr(pairing.urllib.request, "urlopen", urlopen)
    with pytest.raises(ValueError, match="needs an update"):
        pairing.claim_display("192.0.2.8", 8080, "ABCD2345", dict(CONTROLLER))
    assert pairing.list_displays() == []


def test_a_code_the_screen_turned_away_says_check_the_code(monkeypatch):
    def urlopen(req, timeout=10):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                     io.BytesIO(b'{"error": "Invalid or expired code."}'))
    monkeypatch.setattr(pairing.urllib.request, "urlopen", urlopen)
    with pytest.raises(ValueError, match="didn't take that code"):
        pairing.claim_display("192.0.2.8", 8080, "ABCD2345", dict(CONTROLLER))


def test_too_many_wrong_codes_says_to_start_pairing_again(monkeypatch):
    # Checking the code again can never work once the screen stopped listening to this
    # controller: it must say to get a new one.
    code = pairing.start_pairing()
    wrong = "ABCDEFGH" if code != "ABCDEFGH" else "HGFEDCBA"
    for _ in range(pairing.CODE_TRIES):
        with pytest.raises(ValueError):
            pairing.claim(dict(CONTROLLER), pairing.seal_with_code(wrong, b"k"), client="192.0.2.5")
    with pytest.raises(ValueError) as said:
        pairing.claim(dict(CONTROLLER), pairing.seal_with_code(code, b"k"), client="192.0.2.5")

    def urlopen(req, timeout=10):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                     io.BytesIO(json.dumps({"error": str(said.value)}).encode()))
    monkeypatch.setattr(pairing.urllib.request, "urlopen", urlopen)
    with pytest.raises(ValueError, match="Start pairing again on the screen"):
        pairing.claim_display("192.0.2.8", 8080, code, dict(CONTROLLER))


def test_the_claim_route_pairs_without_the_code():
    from fastapi.testclient import TestClient
    from app import config
    from app.server import create_app
    cfg = config.load_config()
    cfg["role"] = "display"
    config.save_config(cfg)
    client = TestClient(create_app())
    code = pairing.start_pairing()
    r = client.post("/api/pair/claim", json={
        "controller": dict(CONTROLLER),
        "sealed_site_key": pairing.seal_with_code(code, b"ab" * 32),
    })
    assert r.status_code == 200 and r.json()["device_id"]
    assert pairing.get_controller()["site_key"] == "ab" * 32
