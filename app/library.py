# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
"""The content library — the ordered list of things a screen shows.

Items are stored locally (so a screen keeps playing even if nothing else on the
network is reachable). Each item is a web page/URL, a Google Slides link, or an
image. A PowerPoint is converted to images on add, then stored as image items.
"""

import json
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config, convert

LIBRARY_PATH = config.DATA / "library.json"
ASSETS = config.DATA / "assets"

DEFAULT_URL_SECONDS = 15
DEFAULT_IMAGE_SECONDS = 10
MIN_SECONDS = 3  # never let an item flash by faster than this


def _load() -> list:
    if LIBRARY_PATH.exists():
        try:
            return json.loads(LIBRARY_PATH.read_text()).get("items", [])
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save(items: list) -> None:
    config.DATA.mkdir(parents=True, exist_ok=True)
    tmp = LIBRARY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"items": items}, indent=2))
    tmp.replace(LIBRARY_PATH)


def _append(item: dict) -> dict:
    items = _load()
    items.append(item)
    _save(items)
    return item


def list_items() -> list:
    return _load()


def _is_google_slides(url: str) -> bool:
    return "docs.google.com/presentation" in url


VIDEO_EXTS = (".mp4", ".webm", ".m4v", ".mov", ".ogg", ".ogv")


def _is_youtube(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def _youtube_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{6,})", url)
    return m.group(1) if m else ""


def _youtube_ref(vid: str, sound: bool = False, cc: bool = True) -> str:
    """A frameable, auto-playing YouTube embed for one video id.

    Muted unless `sound` is on — browsers won't autoplay with sound on a screen
    nobody taps, so sound also needs the kiosk's autoplay-policy flag. cc_load_policy
    forces captions on when `cc` is set; note YouTube auto-shows captions on a muted
    video regardless, so turning them fully off also takes an API call on the screen.
    enablejsapi lets the screen hear (over postMessage) when the video ends, so the
    playlist moves on then instead of cutting it off at a fixed number of seconds.
    No loop= here: the screen loops a lone video itself and lets a video in a list
    end so it can advance."""
    return (f"https://www.youtube.com/embed/{vid}"
            f"?autoplay=1&mute={'0' if sound else '1'}&controls=0"
            f"&rel=0&modestbranding=1&playsinline=1&enablejsapi=1"
            f"&cc_load_policy={'1' if cc else '0'}")


def _youtube_embed(url: str) -> str:
    """The default embed for a freshly added YouTube link: muted, captions on."""
    vid = _youtube_id(url)
    if not vid:
        return url
    return _youtube_ref(vid)


def _is_video_url(url: str) -> bool:
    return url.split("?")[0].lower().endswith(VIDEO_EXTS)


def _slides_autoplay(url: str) -> str:
    """Normalize a 'Publish to web' Slides link for embedding: use the /embed form
    (the /pub full-page view sends X-Frame-Options: SAMEORIGIN and won't render in
    an iframe), ensure it auto-advances, and force loop=true so the deck keeps
    cycling. With loop=false it stops on the last slide, and a deck that ends on a
    dark closing slide then looks like a black screen — looping avoids that."""
    if not _is_google_slides(url):
        return url
    url = url.replace("/pub", "/embed", 1)
    if "start=" not in url:
        sep = "&" if "?" in url else "?"
        url = url + sep + "start=true&delayms=10000"
    if "loop=" in url:
        url = re.sub(r"loop=(?:true|false|1|0)", "loop=true", url)
    else:
        url = url + ("&" if "?" in url else "?") + "loop=true"
    return url


def _slides_per_slide_ms(url: str) -> int:
    """The per-slide auto-advance time baked into a published Slides URL."""
    m = re.search(r"delayms=(\d+)", url)
    return int(m.group(1)) if m else 0


def _chromium() -> str:
    """Path to the same browser the screen already runs, or '' if it isn't here."""
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def _count_slides(url: str) -> int:
    """How many slides are in a published deck. Google no longer lists the slides in
    the published page's HTML, so reading the raw page can't tell us — but the viewer
    still writes 'Slide 1 of N' once it runs. So we open the deck headless in the same
    browser the screen uses and read that number back. Returns 0 on any failure (no
    browser, offline, slow link, layout change) so the caller falls back to letting
    the operator set the time by hand."""
    browser = _chromium()
    if not browser:
        return 0
    # Don't auto-advance while measuring — we only want the 'of N' total, and that
    # doesn't change as the deck plays, so let it sit on the first slide.
    measure_url = re.sub(r"start=(?:true|1)", "start=false", url)
    profile = tempfile.mkdtemp(prefix="eximaro-measure-")
    try:
        dom = subprocess.run(
            [browser, "--headless=new", "--no-sandbox", "--disable-gpu",
             "--disable-dev-shm-usage", "--hide-scrollbars", "--no-first-run",
             "--disable-extensions", f"--user-data-dir={profile}",
             "--virtual-time-budget=8000", "--dump-dom", measure_url],
            capture_output=True, text=True, timeout=45,
        ).stdout
    except Exception:
        return 0
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    m = re.search(r"[Ss]lide\s+\d+\s+of\s+(\d+)", dom)
    n = int(m.group(1)) if m else 0
    return n if 1 <= n <= 500 else 0


def _slides_plan(url: str):
    """(slide_count, per_slide_seconds) for an auto-advancing published deck, or
    (0, 0) when it can't be determined (no delayms, or the deck couldn't be read)."""
    ms = _slides_per_slide_ms(url)
    if ms <= 0:
        return 0, 0
    n = _count_slides(url)
    if n <= 0:
        return 0, 0
    return n, max(ms // 1000, 1)


def add_url(url: str, seconds: int = DEFAULT_URL_SECONDS, name: str = "") -> dict:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if _is_youtube(url) and _youtube_id(url):
        # A single YouTube video times itself: it plays in full, then the screen
        # moves on (seconds=0, like an uploaded video), so the passed-in seconds is
        # ignored and never cuts it off partway. A YouTube URL with no video id
        # (a channel, a playlist, bare youtube.com) is NOT embeddable as a video,
        # so it falls through to be treated as an ordinary web page below. Sound is
        # off and captions on by default; both are per-item toggles (set_sound/set_cc).
        vid = _youtube_id(url)
        return _append({
            "id": secrets.token_hex(4), "type": "url", "ref": _youtube_ref(vid),
            "seconds": 0, "name": name.strip() or "YouTube video",
            "youtube": True, "yt_id": vid, "sound": False, "cc": True,
        })
    if _is_video_url(url):  # a direct link to a video file
        return _append({
            "id": secrets.token_hex(4), "type": "video", "ref": url,
            "seconds": int(seconds), "name": name.strip() or url,
        })
    url = _slides_autoplay(url)
    label = name.strip() or ("Google Slides" if _is_google_slides(url) else url)
    return _append({
        "id": secrets.token_hex(4), "type": "url", "ref": url,
        "seconds": int(seconds), "name": label,
    })


def add_video(filename: str, data: bytes, seconds: int = 0) -> dict:
    """Store an uploaded video file as one item. seconds=0 means play the whole
    video (the screen advances when it ends); a positive value caps it."""
    ASSETS.mkdir(parents=True, exist_ok=True)
    asset_id = secrets.token_hex(8) + (Path(filename).suffix.lower() or ".mp4")
    (ASSETS / asset_id).write_bytes(data)
    return _append({
        "id": secrets.token_hex(4), "type": "video", "ref": asset_id,
        "seconds": int(seconds), "name": filename,
    })


def add_video_path(filename: str, src_path) -> dict:
    """Register an already-on-disk video as one item, moving it into assets. Used
    when a large upload was streamed to a temp file (never buffered whole in RAM)."""
    ASSETS.mkdir(parents=True, exist_ok=True)
    asset_id = secrets.token_hex(8) + (Path(filename).suffix.lower() or ".mp4")
    shutil.move(str(src_path), str(ASSETS / asset_id))
    return _append({
        "id": secrets.token_hex(4), "type": "video", "ref": asset_id,
        "seconds": 0, "name": filename,
    })


def measure_slides(item_id: str) -> dict:
    """Network step, run right after adding a URL: for a Google Slides item, size
    its on-screen time to the whole deck — slide count x the per-slide delay from
    the link — so the deck plays all the way through before the next item instead
    of being cut off after one slide. No-op for non-Slides items or when the deck
    can't be measured (offline, markup change). Kept out of add_url so the
    library core stays pure and offline-testable."""
    target = next((it for it in _load()
                   if it["id"] == item_id and it.get("type") == "url"
                   and _is_google_slides(it.get("ref", ""))), None)
    if target is None:
        return {}
    # The slow part — a headless browser, up to ~45s — runs WITHOUT holding a library
    # snapshot. We re-load fresh afterwards and patch only this one item, so any edit
    # the operator makes during the measurement (this now runs as a background task)
    # isn't clobbered by a stale write, and a deleted item is never resurrected.
    n, per = _slides_plan(target["ref"])
    if not (n and per):
        return target
    items = _load()
    for it in items:
        if it["id"] == item_id:
            it["slides"] = n
            it["per_slide"] = per
            # Size to show EVERY slide: deck time + a buffer for the initial load and
            # Google's own timing drift. loop=true keeps it cycling, so it's never cut off.
            it["seconds"] = (n + 1) * per + 15
            _save(items)
            return it
    return {}   # removed while we were measuring — nothing to write back


def add_image(filename: str, data: bytes, seconds: int = DEFAULT_IMAGE_SECONDS) -> dict:
    ASSETS.mkdir(parents=True, exist_ok=True)
    asset_id = secrets.token_hex(8) + (Path(filename).suffix.lower() or ".img")
    (ASSETS / asset_id).write_bytes(data)
    return _append({
        "id": secrets.token_hex(4), "type": "image", "ref": asset_id,
        "seconds": int(seconds), "name": filename,
    })


def add_pptx(filename: str, data: bytes, seconds: int = DEFAULT_IMAGE_SECONDS) -> dict:
    """Convert a PowerPoint to images and add it as ONE slideshow item that plays
    through all its slides in order (each for `seconds`) before the next playlist
    item — not one row per slide."""
    config.WORK.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)
    work_pptx = config.WORK / (secrets.token_hex(6) + ".pptx")
    work_pptx.write_bytes(data)
    slides_dir = config.WORK / ("slides_" + secrets.token_hex(6))
    try:
        pngs = convert.pptx_to_pngs(work_pptx, slides_dir)
        refs = []
        for png in pngs:
            asset_id = secrets.token_hex(8) + ".png"
            shutil.copyfile(png, ASSETS / asset_id)
            refs.append(asset_id)
        if not refs:
            raise RuntimeError("That PowerPoint produced no slides.")
        return _append({
            "id": secrets.token_hex(4), "type": "slideshow", "refs": refs,
            "seconds": int(seconds), "name": filename, "slides": len(refs),
        })
    finally:
        shutil.rmtree(slides_dir, ignore_errors=True)
        work_pptx.unlink(missing_ok=True)


def _delete_assets(item: dict) -> None:
    """Delete the on-disk files an item owns, so removing it doesn't leave orphans.
    URL / YouTube / direct-link items own no local files."""
    refs = []
    if item.get("type") == "image" and item.get("ref"):
        refs = [item["ref"]]
    elif item.get("type") == "slideshow":
        refs = item.get("refs", [])
    elif item.get("type") == "video" and not str(item.get("ref", "")).startswith("http"):
        refs = [item["ref"]]
    for ref in refs:
        try:
            (ASSETS / ref).unlink()
        except OSError:
            pass


def remove(item_id: str) -> None:
    items = _load()
    gone = next((i for i in items if i["id"] == item_id), None)
    _save([i for i in items if i["id"] != item_id])
    if gone:
        _delete_assets(gone)


def set_targets(item_id: str, targets) -> None:
    """Choose which displays an item plays on. An empty list means every screen
    (the default); a list of device IDs limits it to those displays."""
    chosen = [t for t in (targets or []) if t]
    items = _load()
    for it in items:
        if it["id"] == item_id:
            if chosen:
                it["targets"] = chosen
            else:
                it.pop("targets", None)
            break
    _save(items)


def reorder(order: list) -> None:
    items = _load()
    by_id = {i["id"]: i for i in items}
    kept = set(order)
    new = [by_id[i] for i in order if i in by_id]
    new += [i for i in items if i["id"] not in kept]  # anything not listed stays at the end
    _save(new)


def move(item_id: str, direction: str) -> None:
    """Nudge one item up or down a single slot. Lets an operator fix the play
    order from the panel without removing and re-adding the item."""
    items = _load()
    idx = next((n for n, it in enumerate(items) if it["id"] == item_id), None)
    if idx is None:
        return
    swap = idx - 1 if direction == "up" else idx + 1
    if 0 <= swap < len(items):
        items[idx], items[swap] = items[swap], items[idx]
        _save(items)


def set_seconds(item_id: str, seconds: int) -> None:
    """Change how long one item stays on screen, in place (no re-add needed).
    Videos allow 0 ('play the whole video'); everything else has a small floor."""
    items = _load()
    for it in items:
        if it["id"] == item_id:
            floor = 0 if it.get("type") == "video" or it.get("youtube") else MIN_SECONDS
            it["seconds"] = max(floor, int(seconds))
            break
    _save(items)


def set_sound(item_id: str, on: bool) -> None:
    """Play a video with sound, or mute it. Only videos and YouTube items have
    sound; for YouTube the embed URL is rebuilt so the change reaches the screen.
    Sound needs the kiosk's autoplay-policy flag to actually play on a screen."""
    items = _load()
    for it in items:
        if it["id"] == item_id and (it.get("type") == "video" or it.get("youtube")):
            it["sound"] = bool(on)
            if it.get("youtube") and it.get("yt_id"):
                it["ref"] = _youtube_ref(it["yt_id"], sound=it["sound"], cc=it.get("cc", True))
            break
    _save(items)


def set_cc(item_id: str, on: bool) -> None:
    """Show or hide captions on a YouTube video (captions are on by default).
    Rebuilds the embed URL; the screen also tells the player to hide them, since
    YouTube auto-shows captions on a muted video that a URL param alone won't stop."""
    items = _load()
    for it in items:
        if it["id"] == item_id and it.get("youtube") and it.get("yt_id"):
            it["cc"] = bool(on)
            it["ref"] = _youtube_ref(it["yt_id"], sound=it.get("sound", False), cc=it["cc"])
            break
    _save(items)


def asset_path(ref: str) -> Path:
    return ASSETS / ref
