# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Pure-logic tests for app/updater.py — the app side only writes the request and
reads the status; the privileged swap/rollback lives in install.sh's helper."""

import pytest

from app import updater, __version__

pytestmark = pytest.mark.usefixtures("clean_data_dir")


def test_request_update_writes_default_marker():
    updater.request_update()
    assert updater.REQUEST_PATH.exists()
    assert updater.REQUEST_PATH.read_text().strip() == "latest"
    assert updater.in_progress() is True


def test_request_update_with_source_override():
    updater.request_update("/tmp/some/src")
    assert updater.REQUEST_PATH.read_text().strip() == "/tmp/some/src"


def test_status_is_empty_when_none_written():
    assert updater.status() == {}


def test_current_version_matches_package():
    assert updater.current_version() == __version__


# --- alert: a failed update must never be silent -----------------------------

def _write_status(state, detail="something went wrong"):
    updater.STATUS_PATH.write_text(
        '{"state": "%s", "detail": "%s", "when": "2026-08-14T18:00:00"}' % (state, detail))


def test_alert_empty_with_no_status():
    assert updater.alert() == ""


def test_alert_surfaces_failed_and_rolled_back():
    _write_status("failed")
    assert updater.alert() == "something went wrong"
    _write_status("rolled_back", "rolled back to the previous version")
    assert updater.alert() == "rolled back to the previous version"


def test_alert_silent_on_success_and_progress():
    _write_status("done")
    assert updater.alert() == ""
    _write_status("running")
    assert updater.alert() == ""


def test_alert_has_a_fallback_message():
    _write_status("failed", "")
    assert updater.alert() == "The last update did not complete."


def test_acknowledge_clears_the_alert():
    _write_status("failed")
    assert updater.alert()
    updater.acknowledge()
    assert updater.alert() == ""
    updater.acknowledge()   # idempotent when nothing to clear
