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

import ipaddress
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit

# How long a verdict stands before it is looked up again. Long enough that a screen
# polling every few seconds is not resolving on every tick; short enough that a
# device notices the network coming back without anyone touching it.
CACHE_SECONDS = 20
RESOLVE_TIMEOUT = 2.0
RESOLVE_WAIT = 5.0       # how long a poll waits on a lookup another poll already started

_cache: dict = {}        # host -> (checked_at, reachable)

# Retry discipline for anything that fails. A screen that cannot reach its content
# must not keep knocking: it tries for ACTIVE_WINDOW, then goes QUIET_PERIOD without
# sending a single packet for that target, then tries again. Everything the screen
# does in between is local. This is the owner's rule for a sign on someone else's
# network, and it also keeps a device from feeding Google's throttle.
ACTIVE_WINDOW = 120
QUIET_PERIOD = 900
_failing: dict = {}      # key -> first failure time in the current run of failures
_quiet: dict = {}        # key -> time until which the key is not checked at all


def _gate(key: str, now: float):
    """True when `key` is in a quiet period and must not touch the network."""
    return now < _quiet.get(key, 0)


# Guards the failure records and the running-check list below. Polls are answered side
# by side, so every change to them happens under it.
_lock = threading.Lock()


def _record(key: str, ok: bool, now: float, counts: bool = True) -> None:
    """Track a run of failures; once it has lasted ACTIVE_WINDOW, go quiet. A run left
    over from long ago (nothing was tried since) starts again rather than counting. A
    failure that doesn't count (see STARTUP_GRACE) leaves the record as it was."""
    with _lock:
        if ok:
            _failing.pop(key, None)
            _quiet.pop(key, None)
            return
        if not counts:
            return
        first = _failing.get(key)
        if first is None or now - first > ACTIVE_WINDOW + QUIET_PERIOD:
            first = _failing[key] = now
        if now - first >= ACTIVE_WINDOW:
            _quiet[key] = now + QUIET_PERIOD
            _failing.pop(key, None)


# Straight after a screen is switched on - after a power cut, say - the router often
# hands out addresses minutes before its own internet is up. Until it is, nothing gets
# as far as the content's server, so there is no one to keep quiet for, and a screen
# with slides set up must start them as soon as it can. So for STARTUP_GRACE after
# power-on, a failure where nothing reached the content (the name didn't resolve, or no
# connection was made) doesn't count toward the two minutes: the screen keeps checking
# on the short cycle. Google turning a deck away always counts.
STARTUP_GRACE = 900


def quiet_for(key: str, now: float = None) -> float:
    """Seconds left in a quiet period for this host/url, 0 if it is being checked."""
    now = time.time() if now is None else now
    return max(0.0, _quiet.get(key, 0) - now)


# One check of a host or deck at a time. The screen polls every few seconds and the
# server answers polls side by side, so without this a slow lookup or fetch would be
# started again by the next poll before the first one finished.
_checking: dict = {}     # key -> Event, set when the check running for that key ends


def _begin(key: str):
    """None if the caller should run the check itself, else the Event of the check
    already running for `key`, to wait on instead."""
    with _lock:
        running = _checking.get(key)
        if running is None:
            _checking[key] = threading.Event()
        return running


def _end(key: str) -> None:
    with _lock:
        done = _checking.pop(key, None)
    if done is not None:
        done.set()


_offline = {"since": None}   # when the device last lost its network address


def _note_online(online: bool, now: float) -> None:
    """Stop the two-minute clock while there is no network address. A run of failures
    from before an outage carries on from where it was when the network comes back, so
    the first failed try afterwards isn't taken as two minutes of trying - and a link
    that keeps dropping still adds up its real trying time and goes quiet."""
    with _lock:
        if not online:
            if _offline["since"] is None:
                _offline["since"] = now
            return
        since, _offline["since"] = _offline["since"], None
        if since is not None and now > since:
            for key in list(_failing):
                _failing[key] += now - since

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


def _to_sign_in(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return "accounts.google.com" in host or "/ServiceLogin" in url


class _StopAtSignIn(urllib.request.HTTPRedirectHandler):
    """Follows redirects as usual, except Google's redirect to its sign-in page: that
    redirect IS the answer, so it is read rather than followed. Following it would turn
    Google's refusal into an error wherever accounts.google.com is filtered (common on
    church and school networks) - and an error is asked again much sooner."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _to_sign_in(newurl):
            return None          # urllib then raises HTTPError for this redirect
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_StopAtSignIn)


def _fetch(url: str):
    """(final_url, status) for a GET that follows redirects. Split out so tests can
    stand in for Google. Raises on network failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with _opener.open(req, timeout=PROBE_TIMEOUT) as resp:
            return resp.geturl(), resp.status
    except urllib.error.HTTPError as e:
        with e:
            location = e.headers.get("Location") if e.headers else None
            if 300 <= e.code < 400 and location:
                return urljoin(url, location), e.code
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


def probe(url: str, now: float = None, starting: bool = False):
    """(ok, reason) for a Google Slides deck: is Google serving it to an anonymous
    viewer right now? Cached; a failed network fetch reads as not-ok with a reason.
    `starting`: the screen was switched on less than STARTUP_GRACE ago."""
    now = time.time() if now is None else now
    if _gate(url, now):
        hit = _probe_cache.get(url)
        return False, (hit[2] if hit else "not checked again yet")
    hit = _probe_cache.get(url)
    if hit and (now - hit[0]) < hit[3]:
        return hit[1], hit[2]
    running = _begin(url)
    if running is not None:           # another poll is asking Google right now
        if not hit:                   # nothing to go on yet: wait for its answer
            running.wait(PROBE_TIMEOUT + 2)
            hit = _probe_cache.get(url)
        return (hit[1], hit[2]) if hit else (False, "checking the deck")
    try:
        counts = True
        try:
            ok, reason = _judge(*_fetch(url))
            keep = PROBE_CACHE_SECONDS
        except urllib.error.URLError as exc:
            # The request never got to Google: the name didn't resolve, or no connection
            # was made - the network isn't there yet, which is normal just after a boot.
            # Ask again on the short cycle; the two-minutes-on, fifteen-off rule still
            # decides how long it keeps trying, once the screen is past starting up.
            why = exc.reason if isinstance(exc.reason, BaseException) else exc
            ok, reason = False, f"could not reach Google ({type(why).__name__})"
            keep = CACHE_SECONDS
            counts = not starting
        except Exception as exc:
            # Google had the request, but the answer was too slow or was cut off. That
            # may have counted as a view, so it waits as long as an answer would.
            ok, reason = False, f"Google didn't answer in time ({type(exc).__name__})"
            keep = PROBE_CACHE_SECONDS
        _probe_cache[url] = (now, ok, reason, keep)
        _record(url, ok, now, counts)
        return ok, reason
    finally:
        _end(url)


def last_probe(url: str):
    """Google's last verdict on a deck as (ok, reason), or None if it hasn't been asked
    yet - without asking. The panel uses this, so opening a page can never be what
    makes the device load a deck: only the screen's own rate-limited checks do that."""
    hit = _probe_cache.get(url)
    return (hit[1], hit[2]) if hit else None


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


def reachable(host: str, now: float = None, starting: bool = False) -> bool:
    """Is this host resolvable right now? Cached, so a screen polling every few
    seconds costs one lookup per CACHE_SECONDS rather than one per poll. `starting`:
    the screen was switched on less than STARTUP_GRACE ago."""
    if not host:
        return True                      # local content: nothing to reach
    now = time.time() if now is None else now
    if _gate(host, now):
        return False                 # quiet period: no lookup, last answer stands
    hit = _cache.get(host)
    if hit and (now - hit[0]) < CACHE_SECONDS:
        return hit[1]
    running = _begin(host)
    if running is not None:           # another poll is looking it up right now
        if not hit:                   # nothing to go on yet: wait for its answer
            running.wait(RESOLVE_WAIT)
            hit = _cache.get(host)
        return hit[1] if hit else False
    try:
        ok = _resolves(host)
        _cache[host] = (now, ok)
        _record(host, ok, now, counts=not starting)
        return ok
    finally:
        _end(host)


def _is_local(host: str) -> bool:
    """A page this device serves itself needs no network to show."""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def can_reach_all(urls, now: float = None) -> bool:
    """True when every remote item in the playlist can actually be shown: hosts
    resolve, and every Google Slides deck is being served (not a sign-in wall). A
    playlist with no remote items never triggers a lookup, so a purely local sign
    is silent."""
    return not first_problem(urls, now)


def first_problem(urls, now: float = None, online: bool = True, starting: bool = False) -> str:
    """'' when everything is showable, else a short human reason for the first item
    that isn't — this is what the screen's waiting card and the panel row show.

    `online` is False when the device has no network address at all - before the
    router has answered after a power cut, say. Then there is nothing to ask, so
    nothing is looked up, and the two-minute clock stands still until there is a
    network to try again. `starting` is True for the first STARTUP_GRACE after the
    screen was switched on."""
    now = time.time() if now is None else now
    _note_online(online, now)
    for u in urls:
        h = host_of(u)
        if not h or _is_local(h):
            continue
        if not online:
            return "no network yet"
        if not reachable(h, now, starting):
            return "no network yet"
        if is_slides(u):
            ok, reason = probe(u, now, starting)
            if not ok:
                return reason
    return ""


def reset_cache() -> None:
    """Drop cached verdicts (tests, and after a network change)."""
    _cache.clear()
    _probe_cache.clear()
    with _lock:
        _failing.clear()
        _quiet.clear()
        _checking.clear()
        _offline["since"] = None
