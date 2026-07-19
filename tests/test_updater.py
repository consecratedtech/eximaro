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
