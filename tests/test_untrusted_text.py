# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Text that comes from outside the panel goes on a page as text, never as markup.

Device names arrive from other devices on the network, and anyone on the network can
open these pages when no password is set, so each place is pinned down here.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app import config, discovery

pytestmark = pytest.mark.usefixtures("clean_data_dir")

EVIL = '<img src=x onerror="alert(1)">'


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(discovery, "advertise", lambda *a, **k: None)


def _client(**cfg_extra):
    from app.server import create_app
    cfg = config.load_config()
    cfg.update(cfg_extra)
    config.save_config(cfg)
    return TestClient(create_app())


def test_the_first_run_screen_shows_the_device_name_as_text():
    page = _client(role=None, name=EVIL).get("/").text
    assert EVIL not in page
    assert "&lt;img src=x onerror=" in page


def test_find_displays_puts_network_names_in_as_text():
    page = _client(role="controller").get("/").text
    script = page[page.index("async function findDisplays"):]
    script = script[:script.index("</script>")]
    for raw in ("${x.name||x.address}", "${x.address}", "${x.port}"):
        assert raw not in script
    assert "${esc(x.name||x.address)}" in script and 'value="${esc(x.address)}"' in script


# --- what another device tells us about itself ----------------------------------------

def test_a_displays_id_goes_into_the_screens_page_as_text():
    from app import pairing
    pairing.add_display_record({"device_id": 'x"><b>evil</b>', "name": "Lobby",
                                "address": "192.0.2.8", "port": 8080})
    page = _client(role="controller").get("/").text
    assert '"><b>evil</b>' not in page
    assert 'value="x&quot;&gt;&lt;b&gt;evil&lt;/b&gt;"' in page


class _Reply:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("reply", [
    {"device_id": '"><script>alert(1)</script>', "name": "Lobby"},
    {"device_id": 12345, "name": "Lobby"},
    {"name": "Lobby"},
])
def test_pairing_refuses_a_reply_that_isnt_an_eximaro_screen(monkeypatch, reply):
    from app import pairing
    monkeypatch.setattr(pairing.urllib.request, "urlopen", lambda *a, **k: _Reply(reply))
    with pytest.raises(ValueError):
        pairing.claim_display("192.0.2.8", 8080, "ABCD2345", {"device_id": "c0ffee00"})
    assert pairing.list_displays() == []


def test_pairing_keeps_only_a_text_name(monkeypatch):
    from app import pairing
    monkeypatch.setattr(pairing.urllib.request, "urlopen",
                        lambda *a, **k: _Reply({"device_id": "aaaa1111", "name": ["Lobby"]}))
    record = pairing.claim_display("192.0.2.8", 8080, "ABCD2345", {"device_id": "c0ffee00"})
    assert record["device_id"] == "aaaa1111" and record["name"] == ""


# --- what a push may put on the screen ---------------------------------------------

KEY = "ab" * 32


def _push(items):
    from app import commands, sync
    manifest = {"items": items, "from": "eximaro-c0ffee", "shuffle": False}
    return sync.receive(manifest, commands.sign(KEY, sync.canon(manifest)), KEY)


@pytest.mark.parametrize("bad", [
    "javascript:fetch('/api/password/clear')",
    "JavaScript:alert(1)",
    " javascript:alert(1)",
    "java\tscript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
])
def test_a_push_brings_only_web_links(bad):
    from app import sync
    assert _push([{"type": "url", "url": bad, "seconds": 10},
                  {"type": "url", "url": "https://example.com/", "seconds": 10}])
    assert [i["src"] for i in sync.screen_items()] == ["https://example.com/"]


def test_a_push_never_copies_a_local_file(monkeypatch):
    from app import sync
    fetched = []
    monkeypatch.setattr(sync.urllib.request, "urlretrieve", lambda url, dest: fetched.append(url))
    monkeypatch.setattr(sync, "_download", lambda url, dest: fetched.append(url))
    assert _push([{"type": "image", "asset_url": "file:///var/lib/eximaro/config.json", "seconds": 5},
                  {"type": "slideshow", "asset_urls": ["file:///etc/shadow"], "seconds": 5},
                  {"type": "video", "asset_url": "file:///etc/hostname", "seconds": 5}])
    assert fetched == [] and sync.screen_items() == []


def test_the_screen_never_gets_a_script_link_that_arrived_earlier():
    # A playlist saved before pushes were checked on arrival.
    from app import pairing, sync
    pairing.set_controller({"device_id": "c0ffee00", "name": "eximaro-c0ffee",
                            "address": "192.0.2.5", "site_key": KEY})
    sync._save({"items": [{"type": "url", "src": "javascript:alert(1)", "seconds": 10},
                          {"type": "url", "src": "https://example.com/", "seconds": 10}],
                "shuffle": False})
    data = _client(role="display").get("/api/screen-data").json()
    assert [i["src"] for i in data["items"]] == ["https://example.com/"]
