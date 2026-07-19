# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Shared pytest fixtures and, crucially, environment setup.

``app.config`` reads EXIMARO_DATA / EXIMARO_WORK *at import time* to decide where
data and scratch files live. We therefore point them at a throwaway temp dir
**before any app module is imported** so the test run never reads or writes the
real production data (``/var/lib/eximaro`` or ``~/.local/share/eximaro``).

The directory is created once per test session and removed afterwards.
"""

import os
import sys
import tempfile
from pathlib import Path

# --- set the environment BEFORE importing anything from ``app`` --------------
# This block runs at conftest import, which pytest does before collecting tests,
# so module-level constants such as ``config.DATA`` and ``library.LIBRARY_PATH``
# are computed against the temp dir, not the real one.
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="eximaro-tests-"))
os.environ["EXIMARO_DATA"] = str(_TMP_ROOT / "data")
os.environ["EXIMARO_WORK"] = str(_TMP_ROOT / "work")
# Keep a deterministic role default out of the way unless a test sets it.
os.environ.pop("EXIMARO_ROLE", None)

# Make sure the repo root (which contains the ``app`` package) is importable
# regardless of where pytest is invoked from.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import shutil  # noqa: E402

import pytest  # noqa: E402

# Import config now (env is set) and hard-assert it landed in the temp dir, so a
# misconfigured run fails loudly instead of silently touching real data.
from app import config as _config  # noqa: E402

assert str(_config.DATA).startswith(str(_TMP_ROOT)), (
    f"config.DATA={_config.DATA!r} is not inside the test temp dir {_TMP_ROOT!r}; "
    "refusing to run against real data."
)
assert str(_config.WORK).startswith(str(_TMP_ROOT)), (
    f"config.WORK={_config.WORK!r} is not inside the test temp dir {_TMP_ROOT!r}."
)


def pytest_sessionfinish(session, exitstatus):
    """Remove the temp data dir once the whole session is done."""
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


@pytest.fixture
def tmp_data_root():
    """The session temp root, for tests that want to inspect on-disk files."""
    return _TMP_ROOT


@pytest.fixture
def clean_data_dir():
    """Give a test a pristine data dir: wipe and recreate config.DATA / config.WORK.

    Modules like ``library`` and ``sync`` bound their file paths
    (``LIBRARY_PATH`` etc.) to ``config.DATA`` at import time, so we must clear
    that *same* directory rather than re-pointing the env var.
    """
    for d in (_config.DATA, _config.WORK):
        shutil.rmtree(d, ignore_errors=True)
    _config.DATA.mkdir(parents=True, exist_ok=True)
    _config.WORK.mkdir(parents=True, exist_ok=True)
    yield _config.DATA
    for d in (_config.DATA, _config.WORK):
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def site_key():
    """A deterministic, well-formed 256-bit site key (64 hex chars)."""
    return "ab" * 32
