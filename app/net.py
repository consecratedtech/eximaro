# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
"""Can this device actually reach the content it is about to show?

A screen whose playlist holds a Google Slides deck (or any web link) depends on the
internet being up. On a WiFi device the radio often associates AFTER the app is
already answering, so the kiosk can mount a deck before the network exists — and
Chromium quietly paints its offline error page onto a public sign, where it stays,
because a cross-origin iframe never tells us it failed.

So we ask the question the screen actually needs answered: does the host of the item
we are about to show resolve? That is honest and directly predictive — if DNS for
docs.google.com is not answering, the deck will not load — and it introduces no new
service to phone home to. Results are cached, and nothing is checked at all when a
playlist is entirely local, so an offline-only sign stays completely silent.
"""

import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

# How long a verdict stands before it is looked up again. Long enough that a screen
# polling every few seconds is not resolving on every tick; short enough that a
# device notices the network coming back without anyone touching it.
CACHE_SECONDS = 20
RESOLVE_TIMEOUT = 2.0

_cache: dict = {}        # host -> (checked_at, reachable)

# A Google Slides deck can be *unreachable in a way DNS cannot see*: Google throttles
# anonymous views of a busy published document by answering 401, or redirecting to a
# sign-in page, instead of the deck — and it does so intermittently. Framed in the
# screen's iframe that becomes a Google sign-in button on the wall, and a cross-origin
# frame never tells us. So for a deck we ask Google directly, with a plain browser
# User-Agent, before the screen mounts it. Cached for PROBE_CACHE_SECONDS: every probe
# is itself an anonymous view, so this must stay rare: matched to the screen's
# 15-minute re-mount so a device makes about eight loads an hour in total, not one
# per screen poll.
PROBE_CACHE_SECONDS = 900
PROBE_TIMEOUT = 6.0
_UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/154.0.0.0 Safari/537.36")
_probe_cache: dict = {}  # url -> (checked_at, ok, reason)


def is_slides(url: str) -> bool:
    return "docs.google.com/presentation" in (url or "")


def _fetch(url: str):
    """(final_url, status) for a GET that follows redirects. Split out so tests can
    stand in for Google. Raises on network failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
            return resp.geturl(), resp.status
    except urllib.error.HTTPError as e:
        return e.geturl() or url, e.code


def _judge(final_url: str, status: int):
    host = (urlsplit(final_url).hostname or "").lower()
    if "accounts.google.com" in host or "/ServiceLogin" in final_url:
        return False, "Google is asking viewers to sign in for this deck"
    if status in (401, 403):
        return False, f"Google answered {status} — it wants viewers to sign in for this deck"
    if status != 200:
        return False, f"Google answered {status} for this deck"
    return True, ""


def probe(url: str, now: float = None):
    """(ok, reason) for a Google Slides deck: is Google serving it to an anonymous
    viewer right now? Cached; a failed network fetch reads as not-ok with a reason."""
    now = time.time() if now is None else now
    hit = _probe_cache.get(url)
    if hit and (now - hit[0]) < PROBE_CACHE_SECONDS:
        return hit[1], hit[2]
    try:
        ok, reason = _judge(*_fetch(url))
    except Exception as exc:          # DNS down, timeout, connection refused
        ok, reason = False, f"could not reach Google ({type(exc).__name__})"
    _probe_cache[url] = (now, ok, reason)
    return ok, reason


def host_of(url: str) -> str:
    """The hostname an item will be fetched from, or '' for a local/relative one
    (an uploaded image or video is served by this device and always reachable)."""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return ""
    return (urlsplit(url).hostname or "").lower()


def _resolves(host: str) -> bool:
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(RESOLVE_TIMEOUT)
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return True
    except OSError:
        return False
    finally:
        socket.setdefaulttimeout(old)


def reachable(host: str, now: float = None) -> bool:
    """Is this host resolvable right now? Cached, so a screen polling every few
    seconds costs one lookup per CACHE_SECONDS rather than one per poll."""
    if not host:
        return True                      # local content: nothing to reach
    now = time.time() if now is None else now
    hit = _cache.get(host)
    if hit and (now - hit[0]) < CACHE_SECONDS:
        return hit[1]
    ok = _resolves(host)
    _cache[host] = (now, ok)
    return ok


def can_reach_all(urls, now: float = None) -> bool:
    """True when every remote item in the playlist can actually be shown: hosts
    resolve, and every Google Slides deck is being served (not a sign-in wall). A
    playlist with no remote items never triggers a lookup, so a purely local sign
    is silent."""
    return not first_problem(urls, now)


def first_problem(urls, now: float = None) -> str:
    """'' when everything is showable, else a short human reason for the first item
    that isn't — this is what the screen's waiting card and the panel row show."""
    for u in urls:
        h = host_of(u)
        if not h:
            continue
        if not reachable(h, now):
            return "no network yet"
        if is_slides(u):
            ok, reason = probe(u, now)
            if not ok:
                return reason
    return ""


def reset_cache() -> None:
    """Drop cached verdicts (tests, and after a network change)."""
    _cache.clear()
    _probe_cache.clear()
