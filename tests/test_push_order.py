# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""A push is taken once, by the screen it is meant for.

Pushes cross the network in the clear, signed but not secret. Before this, a push
recorded off the network could be sent to the screen again at any time - putting old
content back, or an emptied playlist - and was taken by any screen of the same site.
That also let a push land on the wrong screen once DHCP handed one screen's old
address to another. Every push now carries the screen it is for and a number that
rises with each push, both inside what is signed.
"""

import io
import json
import urllib.error

import pytest
from fastapi.testclient import TestClient

from app import commands, config, identity, pairing, sync

pytestmark = pytest.mark.usefixtures("clean_data_dir")

KEY = "ab" * 32
CONTROLLER = {"device_id": "c0ffee00", "name": "eximaro-c0ffee",
              "address": "192.0.2.5", "site_key": KEY}
SCREEN = {"device_id": "aaaa1111", "address": "192.0.2.10", "port": 8080}


@pytest.fixture(autouse=True)
def _fresh_counter(monkeypatch):
    monkeypatch.setattr(sync, "_last_seq", 0)


def _client():
    from app.server import create_app
    cfg = config.load_config()
    cfg["role"] = "display"
    config.save_config(cfg)
    return TestClient(create_app())


def _manifest(url, seq=None, to="me"):
    m = {"items": [{"type": "url", "seconds": 15, "url": url}], "from": "eximaro-c0ffee", "shuffle": False}
    if to == "me":
        to = identity.get_or_create_device_id()
    if to is not None:
        m["to"] = to
    if seq is not None:
        m["seq"] = seq
    return m


def _send(client, manifest, key=KEY):
    return client.post("/api/playlist",
                       json={"manifest": manifest, "signature": commands.sign(key, sync.canon(manifest))})


def _showing():
    return [it["src"] for it in (sync.screen_items() or [])]


def _take(seq):
    return pairing.take_push(seq, KEY)


# --- the screen ------------------------------------------------------------------

def test_a_push_played_back_later_is_turned_away():
    pairing.set_controller(dict(CONTROLLER))
    c = _client()
    old = _manifest("https://example.com/old", seq=10)
    assert _send(c, old).status_code == 200
    assert _send(c, _manifest("https://example.com/new", seq=11)).status_code == 200
    r = _send(c, old)                                   # recorded, and sent again later
    assert r.status_code == 409
    assert _showing() == ["https://example.com/new"]
    # It says how new what it has is, vouched for with the site key.
    me = identity.get_or_create_device_id()
    assert r.json()["seq"] == 11
    assert r.json()["proof"] == sync.seq_proof(KEY, 11, me)


def test_the_same_push_twice_is_taken_once():
    pairing.set_controller(dict(CONTROLLER))
    c = _client()
    m = _manifest("https://example.com/a", seq=5)
    assert _send(c, m).status_code == 200
    assert _send(c, m).status_code == 409


def test_a_push_meant_for_another_screen_is_turned_away():
    # Same site key, different screen - or this screen now has the address another
    # one used to have. Nothing is vouched for: the controller must look elsewhere.
    pairing.set_controller(dict(CONTROLLER))
    c = _client()
    assert _send(c, _manifest("https://example.com/mine", seq=1)).status_code == 200
    r = _send(c, _manifest("https://example.com/theirs", seq=2, to="deadbeef"))
    assert r.status_code == 409 and "another screen" in r.json()["error"]
    assert "proof" not in r.json()
    assert _showing() == ["https://example.com/mine"]
    # It says which screen it is, vouched for and tied to that push.
    me = identity.get_or_create_device_id()
    assert r.json()["screen"] == me
    assert r.json()["screen_proof"] == sync.screen_proof(KEY, me, 2)


def test_a_controller_from_before_numbering_still_pushes():
    pairing.set_controller(dict(CONTROLLER))
    c = _client()
    assert _send(c, _manifest("https://example.com/a", to=None)).status_code == 200
    assert _send(c, _manifest("https://example.com/b", to=None)).status_code == 200
    assert _showing() == ["https://example.com/b"]


def test_once_numbered_pushes_came_an_unnumbered_one_is_an_old_one():
    # ...and is refused as "not paired": what an old controller tells its operator then
    # (pair again) is the fix.
    pairing.set_controller(dict(CONTROLLER))
    c = _client()
    assert _send(c, _manifest("https://example.com/a", seq=3)).status_code == 200
    assert _send(c, _manifest("https://example.com/old", to=None)).status_code == 403
    assert _showing() == ["https://example.com/a"]


def test_pairing_again_starts_the_count_afresh():
    pairing.set_controller(dict(CONTROLLER))
    c = _client()
    assert _send(c, _manifest("https://example.com/a", seq=900)).status_code == 200
    pairing.set_controller(dict(CONTROLLER))
    assert _send(c, _manifest("https://example.com/b", seq=1)).status_code == 200


def test_a_forged_number_without_the_key_changes_nothing():
    pairing.set_controller(dict(CONTROLLER))
    c = _client()
    assert _send(c, _manifest("https://example.com/a", seq=2)).status_code == 200
    assert _send(c, _manifest("https://example.com/x", seq=10 ** 15), key="cd" * 32).status_code == 403
    assert _send(c, _manifest("https://example.com/b", seq=3)).status_code == 200


@pytest.mark.parametrize("seq", [0, -5, True, 1.5, "12", 2 ** 53, 10 ** 400])
def test_a_number_that_cant_be_a_push_number_is_refused(seq):
    pairing.set_controller(dict(CONTROLLER))
    c = _client()
    assert _send(c, _manifest("https://example.com/a", seq=seq)).status_code == 409
    assert _showing() == []


def test_a_slower_older_push_does_not_put_older_content_back():
    # Two pushes on their way at once: the older one was checked before the newer one
    # landed, then took longer to fetch. It must not replace the newer content.
    pairing.set_controller(dict(CONTROLLER))
    me = identity.get_or_create_device_id()
    newer = _manifest("https://example.com/new", seq=11)
    assert sync.receive(newer, commands.sign(KEY, sync.canon(newer)), KEY,
                        me=me, last_seq=None, take=_take)
    older = _manifest("https://example.com/old", seq=10)
    with pytest.raises(sync.Refused) as refused:
        sync.receive(older, commands.sign(KEY, sync.canon(older)), KEY,
                     me=me, last_seq=None, take=_take)
    assert refused.value.stale
    assert _showing() == ["https://example.com/new"]


def test_a_push_checked_before_the_screen_was_paired_elsewhere_is_not_taken():
    # Verified against the old controller's key; the screen was paired to another
    # controller while it was still fetching. It must not land, nor set the new count.
    pairing.set_controller(dict(CONTROLLER))
    pairing.set_controller(dict(CONTROLLER, device_id="0dd0dd00", site_key="cd" * 32))
    assert pairing.take_push(1_900_000_000_000, KEY) is False
    assert "last_seq" not in pairing.get_controller()


def test_take_push_needs_a_paired_screen():
    assert pairing.take_push(1, KEY) is False


# --- the controller --------------------------------------------------------------

def _refusal(url, seq, proof=None):
    body = {"error": "this screen already has this push, or a newer one", "seq": seq}
    if proof is not None:
        body["proof"] = proof
    return urllib.error.HTTPError(url, 409, "Conflict", {}, io.BytesIO(json.dumps(body).encode()))


def test_each_push_is_addressed_numbered_and_signed(monkeypatch):
    sent = []
    monkeypatch.setattr(sync, "post_json", lambda url, obj, timeout=10: sent.append(obj) or {"ok": True})
    displays = [SCREEN, dict(SCREEN, device_id="bbbb2222", address="192.0.2.11")]
    items = [{"type": "url", "seconds": 15, "ref": "https://example.com"}]
    for _ in range(2):
        sync.push_targeted(displays, items, "Ctrl", "http://192.0.2.5:8080", KEY)
    assert sorted(o["manifest"]["to"] for o in sent) == ["aaaa1111", "aaaa1111", "bbbb2222", "bbbb2222"]
    for o in sent:
        assert commands.verify(KEY, sync.canon(o["manifest"]), o["signature"])
    by_screen = {}
    for o in sent:
        by_screen.setdefault(o["manifest"]["to"], []).append(o["manifest"]["seq"])
    assert all(a < b for a, b in by_screen.values())
    # One number per push, whichever screens it goes to.
    assert by_screen["aaaa1111"] == by_screen["bbbb2222"]


def test_push_numbers_keep_rising_across_a_restart_with_the_clock_set_back(monkeypatch):
    monkeypatch.setattr(sync.time, "time", lambda: 2_000_000_000.0)
    first = sync.next_seq()
    second = sync.next_seq()
    assert second > first
    monkeypatch.setattr(sync, "_last_seq", 0)                  # the app restarted...
    monkeypatch.setattr(sync.time, "time", lambda: 1_000_000_000.0)   # ...with its clock behind
    assert sync.next_seq() > second


def test_push_numbers_still_rise_if_their_file_is_lost(monkeypatch):
    monkeypatch.setattr(sync.time, "time", lambda: 1_900_000_000.0)
    before = sync.next_seq()
    sync.SEQ_PATH.unlink()
    monkeypatch.setattr(sync, "_last_seq", 0)
    monkeypatch.setattr(sync.time, "time", lambda: 1_900_000_100.0)
    assert sync.next_seq() > before


def test_a_screen_that_already_has_the_push_is_up_to_date(monkeypatch):
    # The push landed, but its answer was lost; or a newer push of ours overtook it.
    def refuse(url, obj, timeout=10):
        seq = obj["manifest"]["seq"]
        raise _refusal(url, seq, sync.seq_proof(KEY, seq, SCREEN["device_id"]))
    monkeypatch.setattr(sync, "post_json", refuse)
    (r,) = sync.push_targeted([SCREEN], [], "Ctrl", "http://192.0.2.5:8080", KEY)
    assert r["ok"]


class Screens:
    """Screens that each hold the newest push number they took, answering like
    /api/playlist does: a push no newer than that is refused, vouched for."""

    def __init__(self, last):
        self.last = dict(last)               # device id -> newest number taken
        self.shown = {}                       # device id -> what it shows
        self.seen = []

    def __call__(self, url, obj, timeout=10):
        m = obj["manifest"]
        to, seq = m["to"], m["seq"]
        self.seen.append((to, seq))
        if seq <= self.last.get(to, 0):
            raise _refusal(url, self.last[to], sync.seq_proof(KEY, self.last[to], to))
        self.last[to] = seq
        self.shown[to] = [it.get("url") for it in m["items"]]
        return {"ok": True}


def _fresh(url):
    def fresh():
        return [{"type": "url", "seconds": 15, "ref": url}], "Ctrl", False, sync.next_seq()
    return fresh


def test_a_screen_ahead_of_the_controller_gets_what_is_here_now_numbered_above(monkeypatch):
    # A controller put back from an old backup, with its clock behind: the screen has
    # taken a higher number from it. Vouched for, so it is sent what is here now, above.
    screens = Screens({"aaaa1111": 9_000_000_000_000})
    monkeypatch.setattr(sync, "post_json", screens)
    (r,) = sync.push_with_rediscovery([SCREEN], [{"type": "url", "seconds": 15, "ref": "https://example.com/then"}],
                                      "Ctrl", "http://192.0.2.5:8080", KEY,
                                      fresh=_fresh("https://example.com/now"))
    assert r["ok"]
    assert screens.shown["aaaa1111"] == ["https://example.com/now"]
    assert screens.last["aaaa1111"] > 9_000_000_000_000


def test_every_screen_ahead_of_the_controller_gets_the_push(monkeypatch):
    # Several screens that all took the same push from before the count was set back:
    # each must get this one - none may be called up to date without it.
    ids = ["aaaa1111", "bbbb2222", "cccc3333", "dddd4444"]
    screens = Screens({i: 1_500_000_000_000 for i in ids})
    monkeypatch.setattr(sync, "post_json", screens)
    monkeypatch.setattr(sync.time, "time", lambda: 1_000_000_000.0)      # clock behind
    displays = [dict(SCREEN, device_id=i, address=f"192.0.2.{20 + n}") for n, i in enumerate(ids)]
    results = sync.push_with_rediscovery(displays, [], "Ctrl", "http://192.0.2.5:8080", KEY,
                                         fresh=_fresh("https://example.com/now"))
    assert all(r["ok"] for r in results)
    assert all(screens.shown.get(i) == ["https://example.com/now"] for i in ids)


def test_a_newer_push_that_got_there_first_leaves_the_newest_content(monkeypatch):
    # Two pushes side by side; the later one (newer content, higher number) lands first.
    # The earlier is refused, and the screen ends up with what is here now.
    screens = Screens({"aaaa1111": 0})
    monkeypatch.setattr(sync, "post_json", screens)
    first = sync.next_seq()
    sync.push_targeted([SCREEN], [{"type": "url", "seconds": 15, "ref": "https://example.com/newer"}],
                       "Ctrl", "http://192.0.2.5:8080", KEY, seq=sync.next_seq())
    (r,) = sync.push_with_rediscovery([SCREEN], [{"type": "url", "seconds": 15, "ref": "https://example.com/older"}],
                                      "Ctrl", "http://192.0.2.5:8080", KEY, seq=first,
                                      fresh=_fresh("https://example.com/newer"))
    assert r["ok"]
    assert screens.shown["aaaa1111"] == ["https://example.com/newer"]


def _wrong_screen(url, me, seq, key=KEY):
    body = {"error": "this push was meant for another screen", "screen": me,
            "screen_proof": sync.screen_proof(key, me, seq)}
    return urllib.error.HTTPError(url, 409, "Conflict", {}, io.BytesIO(json.dumps(body).encode()))


def test_two_screens_that_swapped_addresses_are_found_again(monkeypatch):
    # Where the LAN browse finds nothing: each screen says, vouched for, which screen it
    # is, so the controller can put both right.
    at = {"192.0.2.10": "bbbb2222", "192.0.2.11": "aaaa1111"}   # swapped
    got = {}
    def post(url, obj, timeout=10):
        here = at[url.split("//")[1].split(":")[0]]
        m = obj["manifest"]
        if m["to"] != here:
            raise _wrong_screen(url, here, m["seq"])
        got[here] = url
        return {"ok": True}
    monkeypatch.setattr(sync, "post_json", post)
    moved = []
    displays = [SCREEN, dict(SCREEN, device_id="bbbb2222", address="192.0.2.11")]
    results = sync.push_with_rediscovery(displays, [], "Ctrl", "http://192.0.2.5:8080", KEY,
                                         browse=lambda timeout: [], update=moved.append)
    assert all(r["ok"] for r in results)
    assert got == {"aaaa1111": "http://192.0.2.11:8080/api/playlist",
                   "bbbb2222": "http://192.0.2.10:8080/api/playlist"}
    assert sorted((m["device_id"], m["address"]) for m in moved) == [("aaaa1111", "192.0.2.11"),
                                                                     ("bbbb2222", "192.0.2.10")]


def test_a_screen_s_word_for_another_push_moves_nothing(monkeypatch):
    # The vouched answer is tied to the push it answers: one recorded earlier, played
    # back, can't move a screen's record.
    def post(url, obj, timeout=10):
        raise _wrong_screen(url, "bbbb2222", 1_000_000_000_000)
    monkeypatch.setattr(sync, "post_json", post)
    moved = []
    displays = [SCREEN, dict(SCREEN, device_id="bbbb2222", address="192.0.2.11")]
    sync.push_with_rediscovery(displays, [], "Ctrl", "http://192.0.2.5:8080", KEY,
                               browse=lambda timeout: [], update=moved.append)
    assert moved == []


@pytest.mark.parametrize("seen", [9_000_000_000_000, int("9" * 4300)])
def test_a_number_nobody_vouched_for_never_moves_the_count(monkeypatch, seen):
    # Anything at a screen's old address can answer a push. Its number must not move the
    # controller's count - one far too big once stopped every later push.
    before = sync.next_seq()
    monkeypatch.setattr(sync, "post_json", lambda url, obj, timeout=10: (_ for _ in ()).throw(_refusal(url, seen)))
    (r,) = sync.push_targeted([SCREEN], [], "Ctrl", "http://192.0.2.5:8080", KEY)
    assert not r["ok"]
    assert before < sync.next_seq() < before + 10 ** 6
    monkeypatch.setattr(sync, "post_json", lambda url, obj, timeout=10: {"ok": True})
    (r,) = sync.push_targeted([SCREEN], [], "Ctrl", "http://192.0.2.5:8080", KEY)
    assert r["ok"]


def test_a_vouched_number_from_another_screen_does_not_count(monkeypatch):
    # The proof names the screen: one screen's word can't vouch for another's.
    def refuse(url, obj, timeout=10):
        raise _refusal(url, 9_000_000_000_000, sync.seq_proof(KEY, 9_000_000_000_000, "bbbb2222"))
    monkeypatch.setattr(sync, "post_json", refuse)
    (r,) = sync.push_targeted([SCREEN], [], "Ctrl", "http://192.0.2.5:8080", KEY)
    assert not r["ok"]
    assert sync.next_seq() < 9_000_000_000_000


def test_a_bad_count_on_disk_never_stops_pushes(monkeypatch):
    sync.SEQ_PATH.write_text("9" * 5000)
    monkeypatch.setattr(sync, "post_json", lambda url, obj, timeout=10: {"ok": True})
    (r,) = sync.push_targeted([SCREEN], [], "Ctrl", "http://192.0.2.5:8080", KEY)
    assert r["ok"]


def test_a_different_screen_at_the_address_is_said_plainly():
    text = sync.plain_error("HTTP Error 409: this push was meant for another screen", "192.0.2.10", 8080)
    assert "different screen" in text


def test_a_push_to_a_screen_that_moved_finds_it_again(monkeypatch):
    # The screen's old address now belongs to another screen, which turns the push
    # away; the controller looks for the real one on the network and delivers there,
    # with the same push and number.
    got = {}
    def post(url, obj, timeout=10):
        if "192.0.2.10" in url:
            raise urllib.error.HTTPError(url, 409, "Conflict", {}, io.BytesIO(
                b'{"error": "this push was meant for another screen"}'))
        got[url] = (obj["manifest"]["to"], obj["manifest"]["seq"])
        return {"ok": True}
    monkeypatch.setattr(sync, "post_json", post)
    found = lambda timeout: [{"device_id": "aaaa1111", "address": "192.0.2.20", "port": 8080}]
    (r,) = sync.push_with_rediscovery([SCREEN], [], "Ctrl", "http://192.0.2.5:8080", KEY,
                                      browse=found, seq=1_800_000_000_000)
    assert r["ok"]
    assert got == {"http://192.0.2.20:8080/api/playlist": ("aaaa1111", 1_800_000_000_000)}
