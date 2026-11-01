# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Unpairing a display must take a deliberate second step.

From a church screen: someone changing the week's announcements on the display itself
clicked through a one-line browser popup and unpaired it, and the controller's pushes
were refused for days. These drive the real routes so that can't come back quietly.
"""

import pytest
from fastapi.testclient import TestClient

from app import config, pairing

pytestmark = pytest.mark.usefixtures("clean_data_dir")

CONTROLLER = {"device_id": "c0ffee00", "name": "Sanctuary Controller",
              "address": "192.0.2.5", "site_key": "test-key"}


def _client(role="display"):
    from app.server import create_app
    cfg = config.load_config()
    cfg["role"] = role
    config.save_config(cfg)
    return TestClient(create_app())


def test_a_post_without_the_confirmation_does_not_unpair():
    pairing.set_controller(dict(CONTROLLER))
    r = _client().post("/api/pair/forget", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/pair/forget"
    assert pairing.is_claimed()                       # still paired


def test_the_confirmation_screen_says_what_breaks_and_where_to_go_instead():
    pairing.set_controller(dict(CONTROLLER))
    page = _client().get("/pair/forget").text
    assert "Sanctuary Controller can no longer send anything to this screen" in page
    assert "Every Push to it" in page
    assert "Keep it paired" in page and "Yes, unpair this screen" in page
    assert "http://192.0.2.5:" in page and "/content" in page   # link to the controller


def test_the_explicit_yes_unpairs():
    pairing.set_controller(dict(CONTROLLER))
    r = _client().post("/api/pair/forget", data={"confirm": "yes"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert not pairing.is_claimed()


def test_the_display_home_button_opens_the_screen_not_a_popup():
    pairing.set_controller(dict(CONTROLLER))
    home = _client().get("/").text
    assert 'action="/pair/forget"' in home
    assert "Unpair this screen and remove the pushed content?" not in home   # old popup gone


def test_the_screen_is_harmless_when_not_paired():
    page = _client().get("/pair/forget").text
    assert "isn&#x27;t paired" in page or "isn't paired" in page
