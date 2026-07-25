# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Pure-logic tests for app/library.py.

Covers add_url normalization (bare domain -> https, Slides /pub -> /embed +
autoplay, custom names), reorder semantics, remove, and add_image with a tiny
bytes blob. add_pptx is intentionally NOT tested (needs LibreOffice/poppler).

These tests touch the filesystem, but only inside the temp data dir set up in
conftest.py. The ``clean_data_dir`` fixture wipes that dir before each test so
the persisted library.json never leaks state between tests.
"""

import pytest

from app import library

pytestmark = pytest.mark.usefixtures("clean_data_dir")


# --- add_url: scheme normalization ------------------------------------------

def test_add_url_bare_domain_gets_https():
    item = library.add_url("example.com")
    assert item["ref"] == "https://example.com"
    assert item["type"] == "url"


def test_add_url_http_left_alone():
    item = library.add_url("http://example.com/page")
    assert item["ref"] == "http://example.com/page"


def test_add_url_https_left_alone():
    item = library.add_url("https://secure.example.com")
    assert item["ref"] == "https://secure.example.com"


def test_add_url_strips_whitespace_then_adds_scheme():
    item = library.add_url("  example.com/x  ")
    assert item["ref"] == "https://example.com/x"


def test_add_url_default_seconds_and_generated_id():
    item = library.add_url("example.com")
    assert item["seconds"] == library.DEFAULT_URL_SECONDS
    assert isinstance(item["id"], str) and len(item["id"]) == 8  # token_hex(4)


def test_add_url_seconds_coerced_to_int():
    item = library.add_url("example.com", seconds="25")
    assert item["seconds"] == 25
    assert isinstance(item["seconds"], int)


# --- add_url: name handling -------------------------------------------------

def test_add_url_default_name_is_the_url():
    item = library.add_url("https://example.com/foo")
    assert item["name"] == "https://example.com/foo"


def test_add_url_custom_name_honored():
    item = library.add_url("example.com", name="  Lobby Page  ")
    assert item["name"] == "Lobby Page"


# --- add_url: Google Slides normalization -----------------------------------

SLIDES_PUB = "https://docs.google.com/presentation/d/e/ABC123/pub"


def test_add_url_slides_pub_to_embed_with_autoplay():
    item = library.add_url(SLIDES_PUB)
    assert "/embed" in item["ref"]
    assert "/pub" not in item["ref"]
    assert "start=true" in item["ref"]
    # loop is forced on so the deck keeps cycling instead of parking on its (often
    # dark) closing slide, which would read as a black screen
    assert "loop=true" in item["ref"]
    assert "loop=false" not in item["ref"]
    assert "delayms=10000" in item["ref"]


def test_add_url_slides_forces_loop_true_even_when_user_set_loop_false():
    item = library.add_url(SLIDES_PUB + "?start=true&loop=false&delayms=15000")
    assert "loop=true" in item["ref"]
    assert "loop=false" not in item["ref"]


def test_add_url_slides_default_name_is_google_slides():
    item = library.add_url(SLIDES_PUB)
    assert item["name"] == "Google Slides"


def test_add_url_slides_custom_name_honored():
    item = library.add_url(SLIDES_PUB, name="Weekly Deck")
    assert item["name"] == "Weekly Deck"


def test_add_url_slides_uses_amp_when_query_present():
    item = library.add_url(SLIDES_PUB + "?foo=bar")
    # existing query -> autoplay params appended with '&'
    assert "?foo=bar&start=true" in item["ref"]


def test_add_url_slides_only_replaces_first_pub():
    # .replace(..., 1) -> only the first /pub becomes /embed
    url = "https://docs.google.com/presentation/d/e/X/pub/pub"
    item = library.add_url(url)
    assert item["ref"].count("/embed") == 1
    assert item["ref"].startswith(
        "https://docs.google.com/presentation/d/e/X/embed/pub"
    )


def test_add_url_slides_does_not_duplicate_start_param():
    url = SLIDES_PUB + "?start=false"
    item = library.add_url(url)
    # 'start=' already present -> autoplay params NOT re-added
    assert item["ref"].count("start=") == 1


def test_non_slides_url_unchanged_by_autoplay():
    item = library.add_url("https://example.com/pub")
    # /pub here is not a Slides link, so it must be left exactly alone
    assert item["ref"] == "https://example.com/pub"


# --- add_image --------------------------------------------------------------

def test_add_image_persists_blob_and_metadata():
    data = b"\x89PNG\r\n\x1a\nfake-bytes"
    item = library.add_image("poster.PNG", data)
    assert item["type"] == "image"
    assert item["name"] == "poster.PNG"
    assert item["seconds"] == library.DEFAULT_IMAGE_SECONDS
    # ref preserves the (lowercased) suffix and the file exists with our bytes.
    assert item["ref"].endswith(".png")
    assert library.asset_path(item["ref"]).read_bytes() == data


def test_add_image_no_suffix_gets_img_extension():
    item = library.add_image("noext", b"data")
    assert item["ref"].endswith(".img")


# --- list / append ----------------------------------------------------------

def test_list_items_empty_initially():
    assert library.list_items() == []


def test_added_items_are_listed_in_order():
    a = library.add_url("a.com")
    b = library.add_url("b.com")
    ids = [i["id"] for i in library.list_items()]
    assert ids == [a["id"], b["id"]]


# --- remove -----------------------------------------------------------------

def test_remove_drops_the_right_item():
    a = library.add_url("a.com")
    b = library.add_url("b.com")
    c = library.add_url("c.com")
    library.remove(b["id"])
    remaining = [i["id"] for i in library.list_items()]
    assert remaining == [a["id"], c["id"]]


def test_remove_unknown_id_is_noop():
    a = library.add_url("a.com")
    library.remove("ffffffff")
    assert [i["id"] for i in library.list_items()] == [a["id"]]


# --- reorder ----------------------------------------------------------------

def test_reorder_full_order():
    a = library.add_url("a.com")
    b = library.add_url("b.com")
    c = library.add_url("c.com")
    library.reorder([c["id"], a["id"], b["id"]])
    assert [i["id"] for i in library.list_items()] == [c["id"], a["id"], b["id"]]


def test_reorder_partial_order_unlisted_go_to_end():
    a = library.add_url("a.com")
    b = library.add_url("b.com")
    c = library.add_url("c.com")
    # Only b listed -> b first, then a and c keep their original relative order.
    library.reorder([b["id"]])
    assert [i["id"] for i in library.list_items()] == [b["id"], a["id"], c["id"]]


def test_reorder_ignores_unknown_ids():
    a = library.add_url("a.com")
    b = library.add_url("b.com")
    library.reorder(["does-not-exist", b["id"], "nope", a["id"]])
    assert [i["id"] for i in library.list_items()] == [b["id"], a["id"]]


def test_reorder_empty_order_keeps_all_at_end():
    a = library.add_url("a.com")
    b = library.add_url("b.com")
    library.reorder([])
    assert [i["id"] for i in library.list_items()] == [a["id"], b["id"]]


# --- move (up/down one slot) ------------------------------------------------

def test_move_down_swaps_with_next():
    a = library.add_url("a.com")
    b = library.add_url("b.com")
    c = library.add_url("c.com")
    library.move(a["id"], "down")
    assert [i["id"] for i in library.list_items()] == [b["id"], a["id"], c["id"]]


def test_move_up_swaps_with_previous():
    a = library.add_url("a.com")
    b = library.add_url("b.com")
    library.move(b["id"], "up")
    assert [i["id"] for i in library.list_items()] == [b["id"], a["id"]]


def test_move_up_at_top_is_noop():
    a = library.add_url("a.com")
    b = library.add_url("b.com")
    library.move(a["id"], "up")
    assert [i["id"] for i in library.list_items()] == [a["id"], b["id"]]


def test_move_down_at_bottom_is_noop():
    a = library.add_url("a.com")
    b = library.add_url("b.com")
    library.move(b["id"], "down")
    assert [i["id"] for i in library.list_items()] == [a["id"], b["id"]]


def test_move_unknown_id_is_noop():
    a = library.add_url("a.com")
    library.move("ffffffff", "down")
    assert [i["id"] for i in library.list_items()] == [a["id"]]


# --- set_seconds ------------------------------------------------------------

def test_set_seconds_updates_in_place():
    a = library.add_url("a.com", seconds=10)
    library.set_seconds(a["id"], 45)
    assert library.list_items()[0]["seconds"] == 45


def test_set_seconds_clamps_to_minimum():
    a = library.add_url("a.com", seconds=10)
    library.set_seconds(a["id"], 1)
    assert library.list_items()[0]["seconds"] == library.MIN_SECONDS


def test_set_seconds_coerces_to_int():
    a = library.add_url("a.com", seconds=10)
    library.set_seconds(a["id"], "30")
    assert library.list_items()[0]["seconds"] == 30


def test_set_seconds_unknown_id_is_noop():
    a = library.add_url("a.com", seconds=12)
    library.set_seconds("ffffffff", 99)
    assert library.list_items()[0]["seconds"] == 12


# --- Google Slides per-slide timing (URL parse; no network) ------------------

def test_slides_per_slide_ms_parses_delayms():
    assert library._slides_per_slide_ms(
        "https://docs.google.com/presentation/d/e/X/pub?start=true&loop=true&delayms=15000") == 15000


def test_slides_per_slide_ms_absent_is_zero():
    assert library._slides_per_slide_ms("https://docs.google.com/presentation/d/e/X/pub") == 0


def test_add_url_is_pure_no_slides_metadata_until_measured():
    # add_url must not reach the network; slide count/timing only appear after the
    # separate measure_slides step runs.
    item = library.add_url("https://docs.google.com/presentation/d/e/X/pub?delayms=15000")
    assert "slides" not in item and "per_slide" not in item


def test_measure_slides_unknown_id_is_noop():
    assert library.measure_slides("ffffffff") == {}


# --- video + YouTube --------------------------------------------------------

def test_add_url_youtube_becomes_muted_autoplay_embed():
    item = library.add_url("https://www.youtube.com/watch?v=VIDEOID0001")
    assert item["type"] == "url"
    assert "youtube.com/embed/VIDEOID0001" in item["ref"]
    assert "autoplay=1" in item["ref"] and "mute=1" in item["ref"]
    assert item["name"] == "YouTube video"


def test_add_url_youtube_is_flagged_and_self_timed():
    # A YouTube video plays in full: flagged so the screen times it by its end,
    # with seconds=0 (auto) regardless of any seconds the caller passed.
    item = library.add_url("https://www.youtube.com/watch?v=VIDEOID0001", seconds=30)
    assert item["youtube"] is True
    assert item["seconds"] == 0
    # enablejsapi lets the screen hear the "ended" event; no native loop, so a
    # video in a list can end and advance (the screen loops a lone video itself).
    assert "enablejsapi=1" in item["ref"]
    assert "loop=" not in item["ref"]


def test_add_url_youtube_short_link():
    item = library.add_url("https://youtu.be/abc123XYZ")
    assert "youtube.com/embed/abc123XYZ" in item["ref"]


def test_add_url_youtube_defaults_muted_captions_on():
    item = library.add_url("https://www.youtube.com/watch?v=VIDEOID0001")
    assert item["sound"] is False and item["cc"] is True
    assert item["yt_id"] == "VIDEOID0001"
    assert "mute=1" in item["ref"] and "cc_load_policy=1" in item["ref"]


def test_set_sound_unmutes_youtube_and_rebuilds_embed():
    item = library.add_url("https://www.youtube.com/watch?v=VIDEOID0001")
    library.set_sound(item["id"], True)
    got = library.list_items()[0]
    assert got["sound"] is True
    assert "mute=0" in got["ref"] and "cc_load_policy=1" in got["ref"]  # cc unchanged
    library.set_sound(item["id"], False)
    assert "mute=1" in library.list_items()[0]["ref"]


def test_set_cc_off_rebuilds_youtube_embed():
    item = library.add_url("https://www.youtube.com/watch?v=VIDEOID0001")
    library.set_cc(item["id"], False)
    got = library.list_items()[0]
    assert got["cc"] is False
    assert "cc_load_policy=0" in got["ref"] and "mute=1" in got["ref"]  # sound unchanged


def test_set_sound_on_uploaded_video():
    item = library.add_video("clip.mp4", b"x")
    library.set_sound(item["id"], True)
    assert library.list_items()[0]["sound"] is True


def test_set_sound_and_cc_ignore_non_media_items():
    a = library.add_url("example.com")  # a plain web page, not a video
    library.set_sound(a["id"], True)
    library.set_cc(a["id"], False)
    got = library.list_items()[0]
    assert "sound" not in got and "cc" not in got


def test_add_url_youtube_without_video_id_is_a_plain_page():
    # A channel / playlist / bare youtube.com has no video id, so it isn't an
    # embeddable video — it must NOT be flagged youtube (which would send it to the
    # video player and frame a page YouTube refuses to embed). Treat it as a URL.
    for url in ("https://www.youtube.com/@somechannel",
                "https://www.youtube.com/playlist?list=PLabc123",
                "https://youtube.com"):
        item = library.add_url(url)
        assert not item.get("youtube"), url
        assert item["seconds"] == library.DEFAULT_URL_SECONDS, url


def test_add_url_direct_video_becomes_video_item():
    item = library.add_url("https://example.com/clip.mp4")
    assert item["type"] == "video"
    assert item["ref"] == "https://example.com/clip.mp4"


def test_add_video_stores_asset_and_plays_full_by_default():
    item = library.add_video("promo.MP4", b"\x00\x00\x00\x18ftypmp42")
    assert item["type"] == "video"
    assert item["ref"].endswith(".mp4")
    assert item["seconds"] == 0  # 0 == play the whole video
    assert library.asset_path(item["ref"]).read_bytes().startswith(b"\x00\x00\x00\x18")


def test_set_seconds_video_allows_zero():
    item = library.add_video("v.mp4", b"x")
    library.set_seconds(item["id"], 0)
    assert library.list_items()[0]["seconds"] == 0


def test_set_seconds_nonvideo_still_has_floor():
    a = library.add_url("a.com", seconds=10)
    library.set_seconds(a["id"], 0)
    assert library.list_items()[0]["seconds"] == library.MIN_SECONDS


# --- per-display targeting + asset cleanup ----------------------------------

def test_set_targets_and_clear():
    a = library.add_url("a.com")
    library.set_targets(a["id"], ["dev1", "dev2"])
    assert library.list_items()[0]["targets"] == ["dev1", "dev2"]
    library.set_targets(a["id"], [])           # empty == all screens → field removed
    assert "targets" not in library.list_items()[0]


def test_set_targets_strips_blanks():
    a = library.add_url("a.com")
    library.set_targets(a["id"], ["", "dev1", ""])
    assert library.list_items()[0]["targets"] == ["dev1"]


def test_remove_deletes_image_asset():
    item = library.add_image("p.png", b"\x89PNGdata")
    path = library.asset_path(item["ref"])
    assert path.exists()
    library.remove(item["id"])
    assert not path.exists()


def test_remove_url_item_has_no_asset_and_does_not_error():
    a = library.add_url("a.com")
    library.remove(a["id"])  # no local file to delete; must not raise
    assert library.list_items() == []


def test_add_video_path_moves_file_into_assets(tmp_path):
    src = tmp_path / "clip.webm"
    src.write_bytes(b"\x1aE\xdf\xa3video-bytes")
    item = library.add_video_path("MyClip.WEBM", str(src))
    assert item["type"] == "video"
    assert item["ref"].endswith(".webm")
    assert item["seconds"] == 0
    assert not src.exists()  # moved, not copied (no RAM buffering of large files)
    assert library.asset_path(item["ref"]).read_bytes().startswith(b"\x1aE\xdf\xa3")


# --- measure_slides: re-load after the slow step (no lost updates) -----------

def test_measure_slides_reload_preserves_a_concurrent_edit(monkeypatch):
    """The headless-browser sizing step now runs as an off-request background task,
    so a content edit can land WHILE it runs. measure_slides must re-load the library
    after the slow step and patch only its own item — never write back a stale
    pre-browser snapshot that resurrects a since-removed item."""
    monkeypatch.setattr(library, "_is_google_slides", lambda u: "deckmarker" in (u or ""))
    deck = library.add_url("https://docs.google.com/presentation/deckmarker/pub", 15)
    other = library.add_url("https://example.com/keep-me", 10)

    # Stand in for the slow headless browser; a concurrent request removes 'other'
    # in the middle of it, exactly the window that used to be clobbered.
    def slow_plan(ref):
        library.remove(other["id"])
        return (3, 5)
    monkeypatch.setattr(library, "_slides_plan", slow_plan)

    library.measure_slides(deck["id"])

    items = library.list_items()
    ids = [i["id"] for i in items]
    assert other["id"] not in ids                       # the concurrent removal survives
    deck_now = next(i for i in items if i["id"] == deck["id"])
    assert deck_now["slides"] == 3                       # the deck still got measured
    assert deck_now["seconds"] == (3 + 1) * 5 + 15


def test_measure_slides_does_not_resurrect_a_removed_deck(monkeypatch):
    """If the very deck being measured is removed mid-measurement, its completed
    measurement must not re-create it."""
    monkeypatch.setattr(library, "_is_google_slides", lambda u: "deckmarker" in (u or ""))
    deck = library.add_url("https://docs.google.com/presentation/deckmarker/pub", 15)

    def slow_plan(ref):
        library.remove(deck["id"])
        return (4, 6)
    monkeypatch.setattr(library, "_slides_plan", slow_plan)

    assert library.measure_slides(deck["id"]) == {}     # nothing to write back
    assert deck["id"] not in [i["id"] for i in library.list_items()]
