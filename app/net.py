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
from urllib.parse import urlsplit

# How long a verdict stands before it is looked up again. Long enough that a screen
# polling every few seconds is not resolving on every tick; short enough that a
# device notices the network coming back without anyone touching it.
CACHE_SECONDS = 20
RESOLVE_TIMEOUT = 2.0

_cache: dict = {}        # host -> (checked_at, reachable)


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
    """True when every remote host in the playlist answers. A playlist with no
    remote items never triggers a lookup, so a purely local sign is silent."""
    hosts = {h for h in (host_of(u) for u in urls) if h}
    if not hosts:
        return True
    return all(reachable(h, now) for h in hosts)


def reset_cache() -> None:
    """Drop cached verdicts (tests, and after a network change)."""
    _cache.clear()
