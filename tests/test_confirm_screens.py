# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Every action that changes something important has a real screen first.

The panel is used by people who don't understand technology, so a one-line browser
popup is not enough: it gets clicked through without being read. Each of these
actions now opens a screen that says what will happen - to the TV, to the other
screens, and how to undo it - and the server refuses to act without that screen's
explicit yes. These drive the real routes, so none of it can quietly come back.
"""

import pytest
from fastapi.testclient import TestClient

from app import auth, config, discovery, library, pairing, promote, sessions, sync, updater

pytestmark = pytest.mark.usefixtures("clean_data_dir")

CONTROLLER = {"device_id": "c0ffee00", "name": "eximaro-c0ffee",
              "address": "192.0.2.5", "site_key": "ab" * 32}
LOBBY = {"device_id": "aaaa1111", "name": "Lobby", "address": "192.0.2.8", "port": 8080}
HALL = {"device_id": "bbbb2222", "name": "Hall", "address": "192.0.2.9", "port": 8080}


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    # A role or name change re-advertises the device on the LAN; tests must not.
    monkeypatch.setattr(discovery, "advertise", lambda *a, **k: None)
    pairing.cancel_pairing()
    sessions.clear_all()
    yield
    pairing.cancel_pairing()
    sessions.clear_all()


def _client(role="display", **cfg_extra):
    from app.server import create_app
    cfg = config.load_config()
    cfg["role"] = role
    cfg.update(cfg_extra)
    config.save_config(cfg)
    return TestClient(create_app())


def _go(client, url):
    return client.get(url, follow_redirects=False)


def _post(client, url, **data):
    return client.post(url, data=data, follow_redirects=False)


def _safe_choice_comes_first(page, action):
    """The Keep button's form sits before the yes form, and the yes carries confirm=yes."""
    keep = page.index('<form method="get"', page.index('class="card"'))
    yes = page.index(f'<form method="post" action="{action}"')
    assert keep < yes
    assert 'name="confirm" value="yes"' in page[yes:]


# --- no browser popups anywhere ---------------------------------------------------

def test_no_page_uses_a_browser_popup():
    auth.set_credentials("admin", "secret12")
    pairing.add_display_record(dict(LOBBY))
    controller = _client("controller")
    assert _post(controller, "/login", username="admin", password="secret12").status_code == 303
    pages = [controller.get(p).text for p in ("/", "/content", "/health")]
    assert all('<aside class="drawer"' in p for p in pages)            # signed in, not the login page
    assert 'action="/password/clear"' in pages[0]                      # the locked-panel popup was here
    auth.clear_credentials()
    sessions.clear_all()
    pairing.set_controller(dict(CONTROLLER))
    sync._save({"items": [{"type": "url", "src": "https://example.com", "seconds": 10}], "shuffle": False})
    display = _client("display")
    pages += [display.get(p).text for p in ("/", "/health")]
    assert all('<aside class="drawer"' in p for p in pages)
    for page in pages:
        assert "confirm(" not in page


# --- removing a playlist item -------------------------------------------------------

def test_removing_an_item_needs_the_screen_and_the_screen_changes_nothing():
    item = library.add_image("welcome.png", b"x")
    client = _client("controller")
    r = _post(client, "/api/content/remove", item_id=item["id"])
    assert r.status_code == 303 and r.headers["location"] == f"/content/remove?item_id={item['id']}"
    page = client.get(r.headers["location"]).text
    assert [i["id"] for i in library.list_items()] == [item["id"]]
    assert (library.ASSETS / item["ref"]).exists()
    assert "welcome.png" in page and "Its file is deleted" in page
    assert "Keep it" in page and "Yes, remove it" in page
    _safe_choice_comes_first(page, "/api/content/remove")


def test_the_explicit_yes_removes_the_item_and_its_file():
    item = library.add_image("welcome.png", b"x")
    client = _client("controller")
    r = _post(client, "/api/content/remove", item_id=item["id"], confirm="yes")
    assert r.status_code == 303 and r.headers["location"] == "/content"
    assert library.list_items() == []
    assert not (library.ASSETS / item["ref"]).exists()


def test_a_link_says_it_can_be_added_back_and_shows_the_link_when_the_name_hides_it():
    plain = library.add_url("https://example.com/weekly", 20)
    page = _client("display").get(f"/content/remove?item_id={plain['id']}").text
    assert "add it again" in page
    assert page.count("https://example.com/weekly") == 1          # the title already shows it
    named = library.add_url("https://example.com/news", 20, name="This week")
    page = _client("display").get(f"/content/remove?item_id={named['id']}").text
    assert "add it again later with this link" in page and "https://example.com/news" in page


def test_the_remove_screen_names_the_screens_that_lose_it():
    pairing.add_display_record(dict(LOBBY))
    pairing.add_display_record(dict(HALL))
    item = library.add_url("https://example.com/lobby-only", 20)
    library.set_targets(item["id"], [LOBBY["device_id"]])
    page = _client("controller").get(f"/content/remove?item_id={item['id']}").text
    body = page[page.index('class="card"'):]
    assert "It comes off Lobby." in body and "Hall" not in body.split("Yes, remove it")[0].split("It comes off")[1].split(".")[0]
    assert "Lobby will have nothing left to play" in body and "Waiting for content" in body


def test_the_remove_screen_is_harmless_for_an_item_that_is_gone():
    page = _client("controller").get("/content/remove?item_id=nope").text
    assert "isn&rsquo;t in the playlist any more" in page
    assert 'action="/api/content/remove"' not in page


def test_the_x_opens_the_screen():
    library.add_url("https://example.com/a", 20)
    page = _client("controller").get("/content").text
    assert 'action="/content/remove"' in page and 'action="/api/content/remove"' not in page


# --- a controller unpairing one of its displays ---------------------------------

def test_unpairing_a_display_needs_the_screen():
    pairing.add_display_record(dict(LOBBY))
    client = _client("controller")
    r = _post(client, "/api/displays/remove", device_id=LOBBY["device_id"])
    assert r.headers["location"] == f"/displays/remove?device_id={LOBBY['device_id']}"
    page = client.get(r.headers["location"]).text
    assert [d["device_id"] for d in pairing.list_displays()] == [LOBBY["device_id"]]
    assert "Unpair Lobby?" in page and "keeps showing what it has now" in page
    assert "Re-pair this screen" in page and "http://192.0.2.8:8080/" in page
    assert "Nothing changes on your other screens" in page
    _safe_choice_comes_first(page, "/api/displays/remove")


def test_the_unpair_screen_names_items_that_would_play_nowhere():
    pairing.add_display_record(dict(LOBBY))
    item = library.add_url("https://example.com/lobby-only", 20)
    library.set_targets(item["id"], [LOBBY["device_id"]])
    page = _client("controller").get(f"/displays/remove?device_id={LOBBY['device_id']}").text
    assert "only shows on Lobby" in page and "will show on no screen" in page


def test_the_explicit_yes_unpairs_the_display():
    pairing.add_display_record(dict(LOBBY))
    r = _post(_client("controller"), "/api/displays/remove", device_id=LOBBY["device_id"], confirm="yes")
    assert r.headers["location"] == "/" and pairing.list_displays() == []


def test_the_unpair_screen_is_harmless_for_an_unknown_display():
    page = _client("controller").get("/displays/remove?device_id=nope").text
    assert "isn&rsquo;t paired with" in page and 'action="/api/displays/remove"' not in page


def test_the_screens_page_x_opens_the_screen():
    pairing.add_display_record(dict(LOBBY))
    page = _client("controller").get("/").text
    assert 'action="/displays/remove"' in page and 'action="/api/displays/remove"' not in page


# --- a display dropping what its controller pushed ------------------------------

def _pushed():
    pairing.set_controller(dict(CONTROLLER))
    sync._save({"items": [{"type": "url", "src": "https://example.com", "seconds": 10}], "shuffle": False})


def test_removing_pushed_content_needs_the_screen():
    _pushed()
    client = _client("display")
    r = _post(client, "/api/received/clear")
    assert r.headers["location"] == "/received/clear"
    page = client.get("/received/clear").text
    assert sync.screen_items() is not None
    assert "eximaro-c0ffee" in page and "stays paired" in page and "Push to all displays" in page
    _safe_choice_comes_first(page, "/api/received/clear")


def test_the_explicit_yes_clears_it_but_keeps_the_pairing():
    _pushed()
    _post(_client("display"), "/api/received/clear", confirm="yes")
    assert sync.screen_items() is None and pairing.is_claimed()


def test_the_pushed_screen_is_harmless_with_nothing_pushed():
    page = _client("display").get("/received/clear").text
    assert "Nothing from a controller" in page and 'action="/api/received/clear"' not in page


# --- switching role ---------------------------------------------------------------

def test_the_first_run_choice_needs_no_second_screen(monkeypatch):
    monkeypatch.setattr(promote, "has_conversion_tools", lambda: True)
    client = _client(None)
    r = _post(client, "/api/role", role="display")
    assert r.headers["location"] == "/" and config.load_config()["role"] == "display"


def test_a_configured_display_is_not_switched_without_the_screen(monkeypatch):
    monkeypatch.setattr(promote, "has_conversion_tools", lambda: False)
    pairing.set_controller(dict(CONTROLLER))
    client = _client("display")
    r = _post(client, "/api/role", role="controller")
    assert r.headers["location"] == "/role?role=controller"
    page = client.get(r.headers["location"]).text
    assert config.load_config()["role"] == "display"
    assert not promote.REQUEST_PATH.exists()
    assert "eximaro-c0ffee" in page and "PowerPoint support" in page and "Keep it a Display" in page
    _safe_choice_comes_first(page, "/api/role")


def test_the_explicit_yes_switches_and_keeps_the_pairing(monkeypatch):
    monkeypatch.setattr(promote, "has_conversion_tools", lambda: True)
    pairing.set_controller(dict(CONTROLLER))
    _post(_client("display"), "/api/role", role="controller", confirm="yes")
    assert config.load_config()["role"] == "controller" and pairing.is_claimed()


def test_a_controller_switching_to_display_names_its_displays():
    pairing.add_display_record(dict(LOBBY))
    pairing.add_display_record(dict(HALL))
    page = _client("controller").get("/role?role=display").text
    assert "Lobby and Hall stop getting changes" in page
    assert "Lobby and Hall then get changes from it again" in page


def test_posting_the_same_role_changes_nothing():
    client = _client("display")
    r = _post(client, "/api/role", role="display")
    assert r.headers["location"] == "/" and config.load_config()["role"] == "display"


def test_the_role_button_opens_the_screen():
    page = _client("display").get("/").text
    assert 'action="/role"' in page and 'action="/api/role"' not in page


# --- software update --------------------------------------------------------------

def test_update_needs_the_screen_and_the_screen_starts_nothing():
    client = _client("display")
    r = _post(client, "/api/update")
    assert r.headers["location"] == "/update"
    page = client.get("/update").text
    assert not updater.REQUEST_PATH.exists()
    assert "goes dark for a few seconds, then carries on" in page and "Not now" in page
    assert 'onsubmit="startUpdate();return false;"' in page
    _safe_choice_comes_first(page, "/api/update")


def test_the_explicit_yes_starts_the_update():
    r = _post(_client("display"), "/api/update", confirm="yes")
    assert r.headers["location"] == "/" and updater.REQUEST_PATH.exists()


def test_an_update_already_on_its_way_is_not_offered_again():
    updater.request_update()
    page = _client("display").get("/update").text
    assert "already on its way" in page and 'action="/api/update"' not in page


def test_the_progress_script_sends_the_yes_and_goes_home():
    page = _client("display").get("/").text
    script = page[page.index("async function startUpdate"):]
    script = script[:script.index("</script>")]
    assert "confirm:'yes'" in script and "location.assign('/')" in script
    assert "location.reload()" not in script


# --- password ---------------------------------------------------------------------

def test_removing_the_password_needs_a_sign_in_and_the_screen():
    auth.set_credentials("admin", "secret12")
    client = _client("display")
    assert _go(client, "/password/clear").headers["location"] == "/login"   # locked
    _post(client, "/login", username="admin", password="secret12")
    r = _post(client, "/api/password/clear")
    assert r.headers["location"] == "/password/clear" and auth.has_credentials()
    page = client.get("/password/clear").text
    assert "Anyone on your network" in page and "lock you out" in page and "Keep the password" in page
    _post(client, "/api/password/clear", confirm="yes")
    assert not auth.has_credentials()


# --- a controller's own screen ------------------------------------------------------

def test_turning_a_controllers_screen_off_needs_the_screen_but_on_does_not():
    pairing.add_display_record(dict(LOBBY))
    client = _client("controller")
    r = _post(client, "/api/display-here")                       # unticked = off
    assert r.headers["location"] == "/display-here"
    assert config.load_config().get("display_content", True) is True
    page = client.get("/display-here").text
    assert "Lobby keeps playing" in page and "Show content on this screen" in page
    _post(client, "/api/display-here", confirm="yes")
    assert config.load_config()["display_content"] is False
    _post(client, "/api/display-here", show="on")                # back on, no screen
    assert config.load_config()["display_content"] is True


def test_the_screen_switch_is_not_offered_on_a_display():
    assert _go(_client("display"), "/display-here").headers["location"] == "/"


# --- pairing to a different controller -----------------------------------------------

def test_pairing_an_unpaired_display_is_one_step():
    r = _post(_client("display"), "/api/pair/start")
    assert r.headers["location"] == "/" and pairing.current_code()


def test_re_pairing_a_paired_display_needs_the_screen():
    pairing.set_controller(dict(CONTROLLER))
    client = _client("display")
    r = _post(client, "/api/pair/start")
    assert r.headers["location"] == "/pair/start" and pairing.current_code() is None
    page = client.get("/pair/start").text
    assert "3 minutes" in page and "takes over" in page and "Cancel pairing" in page
    _safe_choice_comes_first(page, "/api/pair/start")
    _post(client, "/api/pair/start", confirm="yes")
    assert pairing.current_code() and pairing.is_claimed()       # paired until someone claims it


# --- PowerPoint support -------------------------------------------------------------

def test_installing_powerpoint_support_needs_the_screen(monkeypatch):
    monkeypatch.setattr(promote, "has_conversion_tools", lambda: False)
    client = _client("controller")
    r = _post(client, "/api/promote")
    assert r.headers["location"] == "/promote" and not promote.REQUEST_PATH.exists()
    page = client.get("/promote").text
    assert "from the internet" in page and "Your screens keep playing" in page
    _safe_choice_comes_first(page, "/api/promote")
    _post(client, "/api/promote", confirm="yes")
    assert promote.REQUEST_PATH.exists()


def test_nothing_to_install_when_it_is_already_there(monkeypatch):
    monkeypatch.setattr(promote, "has_conversion_tools", lambda: True)
    page = _client("controller").get("/promote").text
    assert "already installed" in page and 'action="/api/promote"' not in page


def test_an_update_already_running_is_not_started_again():
    from datetime import datetime, timezone
    updater.STATUS_PATH.write_text('{"state": "running", "detail": "installing dependencies", "when": "%s"}'
                                   % datetime.now(timezone.utc).isoformat(timespec="seconds"))
    client = _client("display")
    assert "already on its way" in client.get("/update").text
    _post(client, "/api/update", confirm="yes")
    assert not updater.REQUEST_PATH.exists()


def test_an_old_running_status_does_not_block_updates():
    updater.STATUS_PATH.write_text('{"state": "running", "detail": "x", "when": "2020-01-01T00:00:00+00:00"}')
    assert 'action="/api/update"' in _client("display").get("/update").text


def test_an_empty_pushed_playlist_is_described_as_it_is():
    pairing.set_controller(dict(CONTROLLER))
    sync._save({"items": [], "shuffle": False})
    page = _client("display").get("/received/clear").text
    assert "sent this screen an empty playlist" in page and "0 item" not in page


def test_a_yes_for_something_already_gone_changes_nothing():
    client = _client("controller")
    r = _post(client, "/api/content/remove", item_id="nope", confirm="yes")
    assert r.headers["location"] == "/content"
    r = _post(client, "/api/displays/remove", device_id="nope", confirm="yes")
    assert r.headers["location"] == "/"


# --- names are escaped -------------------------------------------------------------

def test_names_people_typed_are_shown_as_text_not_markup():
    item = library.add_url("https://example.com/x", 20, name="<b>Easter</b>")
    page = _client("display").get(f"/content/remove?item_id={item['id']}").text
    assert "&lt;b&gt;Easter&lt;/b&gt;" in page and "<b>Easter</b>" not in page
