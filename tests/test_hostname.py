# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Pure-logic tests for app/hostname.py — slugify + unique_label.

Note: apply_hostname() is NOT tested here; it calls os.geteuid()/hostnamectl
(root + Linux only) and is out of scope for pure-logic testing.
"""

import re

from app import hostname

LABEL_RE = re.compile(r"^[a-z0-9-]+$")


# --- slugify ----------------------------------------------------------------

def test_slugify_spaces_to_dash():
    assert hostname.slugify("Foyer TV") == "foyer-tv"


def test_slugify_lowercases_mixed_case():
    assert hostname.slugify("MainLobbyScreen") == "mainlobbyscreen"


def test_slugify_punctuation_collapses():
    # Punctuation runs become a single dash, no leading/trailing dash.
    assert hostname.slugify("Front!!!  Desk???") == "front-desk"


def test_slugify_collapses_repeated_dashes():
    assert hostname.slugify("a---b___c") == "a-b-c"


def test_slugify_strips_leading_trailing_separators():
    assert hostname.slugify("  --Foyer--  ") == "foyer"


def test_slugify_unicode_accents_dropped():
    # Accented/unicode chars are not in [a-z0-9-]; they become separators.
    # 'Café Münster' -> non-ascii removed -> 'caf-m-nster'
    out = hostname.slugify("Café Münster")
    assert LABEL_RE.match(out)
    assert out == "caf-m-nster"


def test_slugify_pure_unicode_falls_back_to_eximaro():
    # Nothing survives the ascii filter -> fallback label, never empty.
    assert hostname.slugify("日本語") == "eximaro"


def test_slugify_empty_string_falls_back():
    assert hostname.slugify("") == "eximaro"
    assert hostname.slugify("   ") == "eximaro"


def test_slugify_only_punctuation_falls_back():
    assert hostname.slugify("!!!???") == "eximaro"


def test_slugify_always_valid_label():
    for alias in ["Foyer TV", "café", "", "  ", "A.B.C", "已被占用", "x" * 200]:
        out = hostname.slugify(alias)
        assert out, f"slugify({alias!r}) returned empty"
        assert LABEL_RE.match(out), f"slugify({alias!r})={out!r} not a valid label"
        assert len(out) <= hostname.HOSTNAME_MAX
        assert not out.startswith("-") and not out.endswith("-")


def test_slugify_length_capped():
    out = hostname.slugify("a" * 200)
    assert len(out) == hostname.HOSTNAME_MAX


# --- unique_label -----------------------------------------------------------

def test_unique_label_no_collision_returns_base():
    assert hostname.unique_label("Foyer TV", "deadbeef00", taken=()) == "foyer-tv"


def test_unique_label_collision_appends_device_suffix():
    # base 'foyer-tv' is taken -> append '-' + device_id[:2]
    out = hostname.unique_label("Foyer TV", "9fabc123", taken={"foyer-tv"})
    assert out == "foyer-tv-9f"


def test_unique_label_suffix_keys_off_device_id():
    a = hostname.unique_label("Foyer TV", "11zzzz", taken={"foyer-tv"})
    b = hostname.unique_label("Foyer TV", "22zzzz", taken={"foyer-tv"})
    assert a == "foyer-tv-11"
    assert b == "foyer-tv-22"
    assert a != b


def test_unique_label_suffix_is_stable():
    # Same alias + same device_id -> same result every time.
    args = ("Foyer TV", "abcd1234", {"foyer-tv"})
    assert hostname.unique_label(*args) == hostname.unique_label(*args)


def test_unique_label_accepts_list_for_taken():
    out = hostname.unique_label("Foyer TV", "abdef", taken=["foyer-tv"])
    assert out == "foyer-tv-ab"


def test_unique_label_result_is_valid_when_base_long():
    # A long base still produces a valid, length-capped label with the suffix.
    out = hostname.unique_label("x" * 200, "ab", taken={"x" * hostname.HOSTNAME_MAX})
    assert LABEL_RE.match(out)
    assert len(out) <= hostname.HOSTNAME_MAX
