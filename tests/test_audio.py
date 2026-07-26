# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Pure-logic tests for app/audio.py.

The app never changes the audio output itself — it is sandboxed and can only write
its data dir — so these cover the two halves it does own: reading the state the root
helper publishes, and writing a well-formed request for the helper to act on.
"""

import json

import pytest

from app import audio

pytestmark = pytest.mark.usefixtures("clean_data_dir")


# --- state (what the helper publishes) --------------------------------------

def test_state_is_empty_when_the_helper_has_not_reported():
    # A fresh device, or one with no sound card: the panel must still render.
    assert audio.state() == {"outputs": [], "current": "", "pinned": ""}


def test_state_survives_a_corrupt_file():
    audio.STATE_PATH.write_text("{not json")
    assert audio.state() == {"outputs": [], "current": "", "pinned": ""}


def test_state_reads_outputs_current_and_pin():
    audio.STATE_PATH.write_text(json.dumps({
        "outputs": [{"id": "36", "name": "Built-in Audio Digital Stereo (HDMI)"},
                    {"id": "68", "name": "Built-in Audio Stereo"}],
        "current": "36",
        "pinned": "USB Audio",
    }))
    out = audio.state()
    assert [o["id"] for o in out["outputs"]] == ["36", "68"]
    assert out["current"] == "36"
    assert out["pinned"] == "USB Audio"


def test_state_fills_in_missing_keys():
    audio.STATE_PATH.write_text(json.dumps({"outputs": None}))
    assert audio.state() == {"outputs": [], "current": "", "pinned": ""}


# --- request (what the panel asks for) --------------------------------------

def test_request_writes_the_choice():
    audio.request("36")
    assert audio.REQUEST_PATH.read_text().strip() == "36"
    assert audio.pending()


def test_request_keeps_a_multi_word_name_on_one_line():
    # A name fragment is a legitimate choice ("USB Audio"), but the helper reads a
    # single line — so inner spaces stay and any newline must not survive.
    audio.request("  USB   Audio\n")
    written = audio.REQUEST_PATH.read_text()
    assert written == "USB Audio\n"
    assert written.count("\n") == 1


def test_request_strips_embedded_newlines():
    audio.request("36\nrm -rf /")
    assert audio.REQUEST_PATH.read_text() == "36 rm -rf /\n"   # one line, never two


def test_empty_request_becomes_a_refresh():
    audio.request("   ")
    assert audio.REQUEST_PATH.read_text().strip() == "refresh"


def test_refresh_helper():
    audio.refresh()
    assert audio.REQUEST_PATH.read_text().strip() == "refresh"


def test_auto_clears_the_pin():
    audio.request(audio.AUTO)
    assert audio.REQUEST_PATH.read_text().strip() == "auto"


def test_pending_is_false_before_any_request():
    assert not audio.pending()


# --- request_output: only ever pin something the device really reported ------

def _seed_outputs():
    audio.STATE_PATH.write_text(json.dumps({
        "outputs": [{"id": "36", "name": "Built-in Audio Digital Stereo (HDMI)"},
                    {"id": "68", "name": "Built-in Audio Stereo"}],
        "current": "36", "pinned": "",
    }))


def test_request_output_pins_by_name_not_by_id():
    # PipeWire re-numbers its nodes every session, so pinning the id would send sound
    # to whatever happened to inherit that number after a reboot.
    _seed_outputs()
    assert audio.request_output("68")
    assert audio.REQUEST_PATH.read_text().strip() == "Built-in Audio Stereo"


def test_request_output_accepts_the_name_the_panel_posts():
    _seed_outputs()
    assert audio.request_output("Built-in Audio Digital Stereo (HDMI)")
    assert audio.REQUEST_PATH.read_text().strip() == "Built-in Audio Digital Stereo (HDMI)"


def test_request_output_allows_auto():
    _seed_outputs()
    assert audio.request_output(audio.AUTO)
    assert audio.REQUEST_PATH.read_text().strip() == "auto"


def test_request_output_refuses_an_unknown_output():
    # A stale page, or a hand-crafted post, must not pin something unusable.
    _seed_outputs()
    assert not audio.request_output("Living Room Soundbar")
    assert not audio.pending()


def test_request_output_refuses_a_regex_that_would_match_anything():
    _seed_outputs()
    assert not audio.request_output(".*")
    assert not audio.pending()


def test_installed_reflects_whether_the_helper_has_reported():
    assert not audio.installed()      # in-app update only replaces app code
    _seed_outputs()
    assert audio.installed()


# --- state(): a malformed file must never take the panel down ---------------

@pytest.mark.parametrize("blob", [
    '"a string"', "[1, 2, 3]", "null", "42",
    '{"outputs": "not-a-list"}',
    '{"outputs": [1, 2]}',
    '{"outputs": [{"id": 36}]}',                       # no name
    '{"outputs": [{"name": "x"}]}',                    # no id
    '{"outputs": [{"id": "1", "name": "   "}]}',       # blank name
    '{"current": {"weird": 1}, "outputs": []}',
])
def test_state_survives_any_shape(blob):
    audio.STATE_PATH.write_text(blob)
    out = audio.state()
    assert isinstance(out["outputs"], list)
    assert all(isinstance(o["id"], str) and o["name"].strip() for o in out["outputs"])
    assert isinstance(out["current"], str) and isinstance(out["pinned"], str)


def test_state_coerces_a_numeric_id_to_text():
    audio.STATE_PATH.write_text('{"outputs": [{"id": 36, "name": "HDMI"}], "current": 36}')
    out = audio.state()
    assert out["outputs"] == [{"id": "36", "name": "HDMI"}]
    assert out["current"] == "36"
