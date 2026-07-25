# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Pure-logic tests for app/sync.py.

Covers canon() determinism, build_manifest shape, the sign->verify round-trip,
and receive() rejecting bad/garbage signatures. The accept path uses only
url-type items so no image download (urlretrieve) is triggered — nothing here
does real HTTP.
"""

import json

import pytest

from app import commands, sync

SITE_KEY = "ab" * 32          # fake but well-formed 256-bit key
OTHER_KEY = "cd" * 32


# --- canon ------------------------------------------------------------------

def test_canon_returns_bytes():
    assert isinstance(sync.canon({"a": 1}), bytes)


def test_canon_is_sorted_and_stable_regardless_of_insertion_order():
    m1 = {"b": 1, "a": 2, "items": [{"y": 1, "x": 2}]}
    m2 = {"items": [{"y": 1, "x": 2}], "a": 2, "b": 1}
    # Top-level key order differs, but canon() sorts keys -> identical bytes.
    assert sync.canon(m1) == sync.canon(m2)


def test_canon_compact_separators():
    out = sync.canon({"a": 1, "b": 2}).decode()
    assert out == '{"a":1,"b":2}'  # no spaces after ':' or ','


def test_canon_distinguishes_different_content():
    assert sync.canon({"a": 1}) != sync.canon({"a": 2})


# --- build_manifest ---------------------------------------------------------

def test_build_manifest_url_item():
    items = [{"type": "url", "seconds": 15, "ref": "https://example.com"}]
    m = sync.build_manifest(items, from_name="Lobby", base_url="http://host:8080")
    assert m["from"] == "Lobby"
    assert m["items"] == [
        {"type": "url", "seconds": 15, "url": "https://example.com"}
    ]


def test_build_manifest_image_item_gets_asset_url():
    items = [{"type": "image", "seconds": 10, "ref": "abc123.png"}]
    m = sync.build_manifest(items, from_name="Lobby", base_url="http://host:8080")
    assert m["items"] == [
        {
            "type": "image",
            "seconds": 10,
            "asset_url": "http://host:8080/asset/abc123.png",
        }
    ]


def test_build_manifest_mixed_and_order_preserved():
    items = [
        {"type": "url", "seconds": 5, "ref": "https://a.com"},
        {"type": "image", "seconds": 8, "ref": "img.png"},
    ]
    m = sync.build_manifest(items, "C", "http://h:1")
    assert [i["type"] for i in m["items"]] == ["url", "image"]


# --- sign + verify round-trip (controller -> display) -----------------------

def test_manifest_sign_verify_round_trip():
    items = [{"type": "url", "seconds": 15, "ref": "https://example.com"}]
    manifest = sync.build_manifest(items, "Lobby", "http://host:8080")
    sig = commands.sign(SITE_KEY, sync.canon(manifest))
    assert commands.verify(SITE_KEY, sync.canon(manifest), sig) is True


def test_manifest_verify_fails_with_wrong_key():
    manifest = sync.build_manifest([], "Lobby", "http://h:1")
    sig = commands.sign(SITE_KEY, sync.canon(manifest))
    assert commands.verify(OTHER_KEY, sync.canon(manifest), sig) is False


def test_signature_survives_json_serialization_roundtrip():
    """A manifest that goes out as JSON and is re-parsed must still verify,
    because canon() re-canonicalizes both sides identically."""
    manifest = sync.build_manifest(
        [{"type": "url", "seconds": 15, "ref": "https://example.com"}],
        "Lobby",
        "http://host:8080",
    )
    sig = commands.sign(SITE_KEY, sync.canon(manifest))
    # Simulate the wire: serialize, ship, parse back (key order may change).
    reparsed = json.loads(json.dumps(manifest))
    assert commands.verify(SITE_KEY, sync.canon(reparsed), sig) is True


# --- receive() reject paths (no disk/network needed) ------------------------

def test_receive_rejects_empty_manifest():
    assert sync.receive({}, "anything", SITE_KEY) is False


def test_receive_rejects_empty_signature():
    manifest = sync.build_manifest([], "C", "http://h:1")
    assert sync.receive(manifest, "", SITE_KEY) is False


def test_receive_rejects_garbage_signature():
    manifest = sync.build_manifest(
        [{"type": "url", "seconds": 15, "ref": "https://example.com"}],
        "C",
        "http://h:1",
    )
    assert sync.receive(manifest, "deadbeef", SITE_KEY) is False


def test_receive_rejects_wrong_key_signature():
    manifest = sync.build_manifest(
        [{"type": "url", "seconds": 15, "ref": "https://example.com"}],
        "C",
        "http://h:1",
    )
    sig = commands.sign(OTHER_KEY, sync.canon(manifest))  # signed with wrong key
    assert sync.receive(manifest, sig, SITE_KEY) is False


# --- receive() accept path (url-only -> no network), writes to temp data dir -

def test_receive_accepts_valid_url_only_manifest(clean_data_dir):
    manifest = sync.build_manifest(
        [{"type": "url", "seconds": 15, "ref": "https://example.com"}],
        "Lobby",
        "http://host:8080",
    )
    sig = commands.sign(SITE_KEY, sync.canon(manifest))
    assert sync.receive(manifest, sig, SITE_KEY) is True
    # Cached playlist is readable and contains the url item (no image fetch).
    cached = sync.screen_items()
    assert cached == [
        {"type": "url", "src": "https://example.com", "seconds": 15}
    ]


def test_screen_items_none_when_nothing_received(clean_data_dir):
    # Fresh data dir -> no received.json yet.
    assert sync.screen_items() is None


# --- per-display targeting --------------------------------------------------

def test_items_for_display_targeting():
    items = [
        {"id": "1", "type": "url", "ref": "x", "seconds": 5},                       # all
        {"id": "2", "type": "url", "ref": "y", "seconds": 5, "targets": ["A"]},     # only A
        {"id": "3", "type": "url", "ref": "z", "seconds": 5, "targets": ["B", "C"]},  # B, C
    ]
    assert [i["id"] for i in sync.items_for_display(items, "A")] == ["1", "2"]
    assert [i["id"] for i in sync.items_for_display(items, "B")] == ["1", "3"]
    assert [i["id"] for i in sync.items_for_display(items, "D")] == ["1"]  # untargeted only


def test_items_for_display_empty_targets_means_all():
    items = [{"id": "1", "type": "url", "ref": "x", "seconds": 5, "targets": []}]
    assert [i["id"] for i in sync.items_for_display(items, "anyone")] == ["1"]


# --- push_targeted (now concurrent across displays) -------------------------

def test_push_targeted_preserves_order_and_isolates_failures(monkeypatch):
    """Pushing to displays concurrently must not scramble result order or let one
    unreachable display sink the others: results line up with the input displays,
    and a display whose POST raises comes back ok=False while the rest stay ok."""
    def fake_post(url, obj, timeout=10):
        if "10.0.0.2" in url:            # the middle display is unreachable
            raise OSError("connection refused")
        return {"ok": True}
    monkeypatch.setattr(sync, "post_json", fake_post)

    displays = [
        {"device_id": "d1" * 8, "name": "one",   "address": "10.0.0.1", "port": 8080},
        {"device_id": "d2" * 8, "name": "two",   "address": "10.0.0.2", "port": 8080},
        {"device_id": "d3" * 8, "name": "three", "address": "10.0.0.3", "port": 8080},
    ]
    items = [{"type": "url", "seconds": 10, "ref": "https://example.com"}]
    results = sync.push_targeted(displays, items, "Ctrl", "http://10.0.0.9:8080", SITE_KEY)

    assert [r["name"] for r in results] == ["one", "two", "three"]   # order preserved
    assert [r["ok"] for r in results] == [True, False, True]         # failure isolated
    assert "error" in results[1]


def test_push_targeted_empty_displays_returns_empty():
    items = [{"type": "url", "seconds": 5, "ref": "x"}]
    assert sync.push_targeted([], items, "C", "http://x:8080", SITE_KEY) == []


# --- clear_received (take pushed content down from the display) --------------

def test_clear_received_removes_playlist_and_assets(clean_data_dir):
    sync._save({"items": [{"type": "url", "src": "https://x", "seconds": 5}]})
    sync.RECV_ASSETS.mkdir(parents=True, exist_ok=True)
    (sync.RECV_ASSETS / "a.img").write_bytes(b"x")
    assert sync.screen_items() is not None
    sync.clear_received()
    assert sync.screen_items() is None
    assert not (sync.RECV_ASSETS / "a.img").exists()
