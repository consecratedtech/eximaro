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
