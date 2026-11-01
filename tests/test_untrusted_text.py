# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Text that comes from outside the panel goes on a page as text, never as markup.

Device names arrive from other devices on the network, and anyone on the network can
open these pages when no password is set, so each place is pinned down here.
"""

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
