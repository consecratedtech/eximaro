# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""A paired display's page: who controls it, where the pairing controls live, the
second step before unpairing, and names that follow renames.

From a church screen: someone swapping the week's announcements on the display itself
clicked through a one-line browser popup and unpaired it, and the controller's pushes
were refused for days. The page also named its controller by whatever it was called
on pairing day. These drive the real routes so none of that can come back quietly.
"""

import pytest
from fastapi.testclient import TestClient

from app import commands, config, pairing, sync

pytestmark = pytest.mark.usefixtures("clean_data_dir")

KEY = "ab" * 32                       # site keys are hex
CONTROLLER = {"device_id": "c0ffee00", "name": "eximaro-c0ffee",
              "address": "192.0.2.5", "site_key": KEY}


def _client(role="display", peer=None):
    """A client for the real app. `peer` is the IP its requests come from; left out,
    they come from the test client's placeholder name, which is not an address."""
    from app.server import create_app
    cfg = config.load_config()
    cfg["role"] = role
    config.save_config(cfg)
    if peer:
        return TestClient(create_app(), client=(peer, 50000))
    return TestClient(create_app())


def _split(html):
    """(page body, settings drawer), so a test can say WHERE a control is."""
    i = html.index('<aside class="drawer"')
    return html[:i], html[i:]


def _push(client, from_name, key=KEY, items=()):
    manifest = {"items": list(items), "from": from_name, "shuffle": False}
    return client.post("/api/playlist",
                       json={"manifest": manifest, "signature": commands.sign(key, sync.canon(manifest))})


# --- the second step before unpairing ------------------------------------------

def test_a_post_without_the_confirmation_does_not_unpair():
    pairing.set_controller(dict(CONTROLLER))
    r = _client().post("/api/pair/forget", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/pair/forget"
    assert pairing.is_claimed()                       # still paired


def test_the_confirmation_screen_says_what_breaks_and_where_to_go_instead():
    pairing.set_controller(dict(CONTROLLER))
    page = _client().get("/pair/forget").text
    assert "eximaro-c0ffee can no longer send anything to this screen" in page
    assert "Every Push to it" in page
    assert "Keep it paired" in page and "Yes, unpair this screen" in page
    assert "http://192.0.2.5:" in page and "/content" in page   # link to the controller


def test_the_explicit_yes_unpairs():
    pairing.set_controller(dict(CONTROLLER))
    r = _client().post("/api/pair/forget", data={"confirm": "yes"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert not pairing.is_claimed()


def test_the_screen_is_harmless_when_not_paired():
    page = _client().get("/pair/forget").text
    assert "isn&#x27;t paired" in page or "isn't paired" in page


# --- where things live on a paired display's page -------------------------------

def test_unpair_and_repair_live_in_settings_not_on_the_page():
    pairing.set_controller(dict(CONTROLLER))
    body, drawer = _split(_client().get("/").text)
    assert 'action="/pair/forget"' in drawer and 'action="/pair/forget"' not in body
    assert 'action="/pair/start"' in drawer and 'action="/pair/start"' not in body
    assert 'action="/api/pair/start"' not in body + drawer          # re-pair asks first too
    assert "Unpair this screen and remove the pushed content?" not in body + drawer   # old popup


def test_the_top_says_who_controls_it_and_at_what_address():
    pairing.set_controller(dict(CONTROLLER))
    body, _ = _split(_client().get("/").text)
    assert "Controlled by eximaro-c0ffee" in body
    assert "192.0.2.5" in body


def test_remove_pushed_content_sits_below_the_content():
    pairing.set_controller(dict(CONTROLLER))
    sync._save({"items": [{"type": "url", "src": "https://example.com", "seconds": 10}],
                "shuffle": False})
    body, _ = _split(_client().get("/").text)
    assert body.index('action="/received/clear"') > body.index("<h2>Playlist")
    assert body.index("Controlled by") < body.index("<h2>Playlist")     # who-controls stays on top


def test_the_page_stops_inviting_edits_that_will_not_show():
    pairing.set_controller(dict(CONTROLLER))
    sync._save({"items": [], "shuffle": False})
    body, _ = _split(_client().get("/").text)
    assert "only shows if that content is removed" in body


# --- names and addresses follow the real devices ----------------------------------

def test_a_verified_push_keeps_the_controller_name_and_address_current():
    pairing.set_controller(dict(CONTROLLER))          # paired when it was eximaro-c0ffee
    client = _client(peer="192.0.2.77")               # ...since renamed, on a new lease
    r = _push(client, "eximaro-sanctuary")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["name"]                           # the display answers with its own name
    c = pairing.get_controller()
    assert c["name"] == "eximaro-sanctuary"
    assert c["address"] == "192.0.2.77"               # where the push actually came from
    body, drawer = _split(client.get("/").text)
    assert "Controlled by eximaro-sanctuary" in body and "192.0.2.77" in body
    assert "eximaro-sanctuary" in drawer


def test_an_address_no_one_could_open_is_never_stored():
    pairing.set_controller(dict(CONTROLLER))
    r = _push(_client(), "eximaro-sanctuary")         # from the placeholder "testclient"
    assert r.status_code == 200
    c = pairing.get_controller()
    assert c["name"] == "eximaro-sanctuary"           # the name still follows
    assert c["address"] == "192.0.2.5"                # the good address is kept
    for bad in ("testclient", "127.0.0.1", "0.0.0.0", "fe80::1", "", None, 42):
        assert pairing.lan_address(bad) == "", bad
    assert pairing.lan_address("192.0.2.9") == "192.0.2.9"
    assert pairing.lan_address("::ffff:192.0.2.9") == "192.0.2.9"


def test_a_push_landing_just_after_an_unpair_does_not_pair_it_again():
    pairing.set_controller(dict(CONTROLLER))
    pairing.forget_controller()
    assert pairing.refresh_controller(name="eximaro-sanctuary", address="192.0.2.77") is False
    assert not pairing.is_claimed()


def test_a_forged_push_changes_nothing():
    pairing.set_controller(dict(CONTROLLER))
    r = _push(_client(), "someone-else", key="cd" * 32)  # wrong key
    assert r.status_code == 403
    c = pairing.get_controller()
    assert c["name"] == "eximaro-c0ffee" and c["address"] == "192.0.2.5"


def test_a_renamed_display_is_remembered_but_a_removed_one_never_comes_back():
    rec = {"device_id": "aaaa1111", "name": "eximaro-decaf0", "address": "192.0.2.8", "port": 8080}
    pairing.add_display_record(dict(rec))
    assert pairing.rename_display("aaaa1111", "eximaro-lobby") is True
    assert pairing.list_displays()[0]["name"] == "eximaro-lobby"
    assert pairing.list_displays()[0]["address"] == "192.0.2.8"       # nothing else changes
    assert pairing.rename_display("aaaa1111", "eximaro-lobby") is False  # no needless write
    pairing.remove_display("aaaa1111")                                  # unpaired mid-push...
    assert pairing.rename_display("aaaa1111", "eximaro-lobby") is False  # ...stays unpaired
    assert pairing.list_displays() == []


def test_changes_made_at_the_same_moment_are_never_lost():
    # Pushes, renames and unpairs run on different threads. Each must see the
    # others' changes, not save over them with a stale copy.
    import threading
    ids = [f"dev{i:02d}" for i in range(24)]
    start = threading.Barrier(len(ids))
    errors = []

    def add(device_id):
        try:
            start.wait()
            pairing.add_display_record({"device_id": device_id, "name": device_id,
                                        "address": "192.0.2.8", "port": 8080})
        except Exception as exc:                      # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(d["device_id"] for d in pairing.list_displays()) == ids


def test_refresh_controller_writes_only_on_a_change():
    pairing.set_controller(dict(CONTROLLER))
    assert pairing.refresh_controller(name="eximaro-c0ffee", address="192.0.2.5") is False
    assert pairing.refresh_controller(name="", address="") is False      # nothing to learn
    assert pairing.refresh_controller(name="eximaro-new", address="") is True
    assert pairing.get_controller()["address"] == "192.0.2.5"            # untouched
