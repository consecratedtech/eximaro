# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
"""Pushing content from a controller to its displays.

The controller builds a manifest of its playlist, signs it with the site key,
and sends it to each paired display. A display verifies the signature against
the controller it trusts, downloads any images, and caches everything locally —
so it keeps playing even if the controller later goes offline.
"""

import hashlib
import json
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import commands, config

RECEIVED_PATH = config.DATA / "received.json"
RECV_ASSETS = config.DATA / "recv_assets"
SEQ_PATH = config.DATA / "push-seq"      # controller side: the number of its last push
# Push numbers count milliseconds; anything this large is not one (and a far larger one
# would not even turn back into text).
MAX_SEQ = 2 ** 53

_SEQ_LOCK = threading.Lock()
_last_seq = 0
_SAVE_LOCK = threading.Lock()   # display side: one new playlist replaces the old at a time


class Refused(Exception):
    """A push signed by this screen's controller that the screen still won't take. The
    message says why. `stale` means it has this push or a newer one already; `seq`, if
    known, is the newest it has taken."""

    def __init__(self, why: str, status: int = 409, stale: bool = False, seq=None,
                 misaddressed: bool = False):
        super().__init__(why)
        self.status = status
        self.stale = stale
        self.seq = seq
        self.misaddressed = misaddressed


def canon(manifest: dict) -> bytes:
    """Stable byte form both sides sign/verify identically."""
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()


def web_url(value) -> bool:
    """Is this an ordinary http(s) link? A push may only bring those: a javascript:
    link would run code inside the screen's own page, and a file: one would copy
    files off this device into its open asset folder."""
    return (isinstance(value, str) and value.lower().startswith(("http://", "https://"))
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def post_json(url: str, obj: dict, timeout: int = 10) -> dict:
    data = json.dumps(obj).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


ASSET_TIMEOUT = 20                       # seconds before a stalled asset fetch gives up
MAX_ASSET_BYTES = 600 * 1024 * 1024      # 600 MB cap per asset (a wedged/huge file can't hang us)


def _download(url: str, dest: Path) -> None:
    """Fetch an asset to dest with a timeout and a size cap, streaming in chunks so
    a stalled or oversized push can never block the receive path forever. Raises on
    failure (the caller skips that one item and keeps the rest)."""
    req = urllib.request.Request(url, headers={"User-Agent": "eximaro"})
    try:
        with urllib.request.urlopen(req, timeout=ASSET_TIMEOUT) as resp, open(dest, "wb") as f:
            total = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ASSET_BYTES:
                    raise ValueError("asset exceeds size cap")
                f.write(chunk)
    except Exception:
        try:
            dest.unlink()
        except OSError:
            pass
        raise


# --- controller side --------------------------------------------------------

def build_manifest(items: list, from_name: str, base_url: str, shuffle: bool = False) -> dict:
    """Turn the controller's library into a manifest the display can play.
    Images are referenced by a URL back to the controller so the display can
    fetch and cache them."""
    out = []
    for it in items:
        if it["type"] == "url":
            entry = {"type": "url", "seconds": it["seconds"], "url": it["ref"]}
            if it.get("youtube"):
                entry["youtube"] = True  # so a display plays it in full, not for N seconds
                entry["sound"] = bool(it.get("sound"))
                entry["cc"] = it.get("cc", True)
            out.append(entry)
        elif it["type"] == "slideshow":
            out.append({
                "type": "slideshow", "seconds": it["seconds"],
                "asset_urls": [f"{base_url}/asset/{r}" for r in it.get("refs", [])],
            })
        elif it["type"] == "video":
            if it["ref"].startswith("http"):  # a direct video link plays as-is
                entry = {"type": "video", "seconds": it["seconds"], "url": it["ref"]}
            else:
                entry = {"type": "video", "seconds": it["seconds"],
                         "asset_url": f"{base_url}/asset/{it['ref']}"}
            if it.get("sound"):
                entry["sound"] = True
            out.append(entry)
        else:
            out.append({
                "type": "image", "seconds": it["seconds"],
                "asset_url": f"{base_url}/asset/{it['ref']}",
            })
    # shuffle rides along with the playlist: it is part of how the controller wants
    # this content played, not a setting of the screen showing it. An older display
    # simply ignores the extra field.
    return {"items": out, "from": from_name, "shuffle": bool(shuffle)}


def _store_seq(seq: int) -> None:
    global _last_seq
    _last_seq = max(_last_seq, seq)
    try:
        config.DATA.mkdir(parents=True, exist_ok=True)
        tmp = SEQ_PATH.with_suffix(".tmp")
        tmp.write_text(str(_last_seq))
        tmp.replace(SEQ_PATH)
    except OSError as exc:
        print(f"push: could not save the number of the last push: {exc}", file=sys.stderr, flush=True)


def seq_ok(value) -> bool:
    """Could this be a push number?"""
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < MAX_SEQ


def _stored_seq() -> int:
    try:
        seq = int(SEQ_PATH.read_text().strip())
    except (OSError, ValueError):
        return 0
    return seq if seq_ok(seq) else 0


def highest_seq() -> int:
    """The highest number this controller has given a push."""
    return max(_stored_seq(), _last_seq)


def next_seq() -> int:
    """The number for the next push: higher than any this controller has used before,
    across restarts too, so a display can tell a new push from an old one played back to
    it. It is kept in a file, and never below the clock in milliseconds, so it still
    rises should that file ever be lost."""
    with _SEQ_LOCK:
        seq = max(highest_seq() + 1, int(time.time() * 1000))
        _store_seq(seq)
        return seq


def seq_proof(site_key: str, seq: int, device_id: str) -> str:
    """Display side: vouches, with the site key, for the newest push number it has
    taken, so that its controller can trust that number and no one else's."""
    return commands.sign(site_key, f"seq:{seq}:{device_id}".encode())


def _vouched_seq(answer: dict, site_key: str, device_id: str):
    """The newest push number a display says it has taken, if it vouched for it with
    the site key - else None."""
    seq, proof = answer.get("seq"), answer.get("proof")
    if seq_ok(seq) and isinstance(proof, str) and commands.verify(
            site_key, f"seq:{seq}:{device_id}".encode(), proof):
        return seq
    return None


def screen_proof(site_key: str, device_id: str, seq: int) -> str:
    """Display side, turning away a push numbered `seq` that was meant for another
    screen: vouches, with the site key, for which screen it is, so the controller can
    tell where its screens answer now."""
    return commands.sign(site_key, f"screen:{device_id}:{seq}".encode())


def _vouched_screen(answer: dict, site_key: str, seq: int):
    """The device id of the screen that answered a push numbered `seq` meant for
    another, if it vouched for it with the site key - else None. Bound to this push's
    number, so an answer recorded earlier can't be played back."""
    me, proof = answer.get("screen"), answer.get("screen_proof")
    if (isinstance(me, str) and 0 < len(me) <= 64 and isinstance(proof, str)
            and commands.verify(site_key, f"screen:{me}:{seq}".encode(), proof)):
        return me
    return None


def note_seq(seen: int) -> None:
    """A display vouched that it has taken a push numbered `seen` from this controller,
    above any this controller remembers giving (it was put back from an old backup, say,
    with its clock behind): number the next push above it."""
    if seq_ok(seen):
        with _SEQ_LOCK:
            if seen > highest_seq():
                _store_seq(seen)


def push_all(displays: list, manifest: dict, site_key: str) -> list:
    signature = commands.sign(site_key, canon(manifest))
    results = []
    for d in displays:
        url = f"http://{d['address']}:{d['port']}/api/playlist"
        try:
            post_json(url, {"manifest": manifest, "signature": signature})
            results.append({"name": d.get("name") or d["device_id"][:8], "ok": True})
        except Exception as exc:
            results.append({"name": d.get("name") or d["device_id"][:8], "ok": False, "error": str(exc)})
    return results


def items_for_display(items: list, device_id: str) -> list:
    """The items a given display should play: untargeted items (shown on every
    screen) plus items that explicitly target this display."""
    return [it for it in items if not it.get("targets") or device_id in it["targets"]]


def push_targeted(displays: list, items: list, from_name: str, base_url: str, site_key: str,
                  shuffle: bool = False, seq=None) -> list:
    """Per-display targeting: build each display its own manifest from the items
    aimed at it, sign it, and push. Untargeted items stack onto every display.

    Every manifest is addressed and numbered, inside what is signed: a display takes a
    push only if it is meant for that display and newer than the last one it took, so a
    push recorded off the network can't be played back to it later, or to another
    screen - nor does one land on whichever screen has since been given the address
    this one used to have. `seq` is this push's number; the caller takes it when it
    reads `items` (next_seq), so a later push always carries newer content.

    Displays are pushed concurrently, so one slow or offline display can't hold up
    the rest — the whole push takes as long as the slowest single display, not the
    sum of them all. Result order matches the input display order."""
    if seq is None:
        seq = next_seq()

    def _send(d: dict, mine: list, number: int):
        manifest = build_manifest(mine, from_name, base_url, shuffle)
        manifest["to"] = d["device_id"]
        manifest["seq"] = number
        signature = commands.sign(site_key, canon(manifest))
        return post_json(f"http://{d['address']}:{d['port']}/api/playlist",
                         {"manifest": manifest, "signature": signature})

    def _one(d: dict) -> dict:
        name = d.get("name") or d["device_id"][:8]
        mine = items_for_display(items, d["device_id"])
        try:
            resp = _send(d, mine, seq)
        except urllib.error.HTTPError as exc:
            said = _answer(exc)
            why = said.get("error") if isinstance(said.get("error"), str) else exc.reason
            failed = {"name": name, "ok": False, "count": len(mine), "error": f"HTTP Error {exc.code}: {why}"}
            if exc.code != 409:
                return failed
            seen = _vouched_seq(said, site_key, d["device_id"])
            if seen == seq:
                # It has this very push already: an earlier try got there.
                return {"name": name, "ok": True, "count": len(mine), "display_name": None}
            if seen is not None and seen > seq:
                # It has a push numbered above this one: a newer push of ours got there
                # first, or this controller's count was set back (put back from an old
                # backup, say, with its clock behind). Either way it is to be sent what is
                # here now, under a number above what it has (push_with_rediscovery).
                note_seq(seen)
                return dict(failed, ahead=seen)
            other = _vouched_screen(said, site_key, seq)
            return dict(failed, elsewhere=other) if other else failed
        except Exception as exc:
            return {"name": name, "ok": False, "error": str(exc)}
        # The display answers with the name it has NOW (absent on an older one).
        fresh = resp.get("name") if isinstance(resp, dict) else None
        return {"name": name, "ok": True, "count": len(mine), "display_name": fresh}

    if not displays:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(displays))) as pool:
        return list(pool.map(_one, displays))


def _answer(exc: urllib.error.HTTPError) -> dict:
    """The JSON a display sent with an error, or {}."""
    try:
        said = json.loads(exc.read().decode())
    except Exception:
        return {}
    return said if isinstance(said, dict) else {}


def plain_error(exc_text: str, address: str, port) -> str:
    """A push error an operator can act on, instead of a Python exception string."""
    t = (exc_text or "").lower()
    if "http error 403" in t or "http error 401" in t:
        return (f"the screen at {address} refused the push: it is no longer paired to this "
                f"controller. Pair it again from the Screens page")
    if "http error 409" in t and "another screen" in t:
        return (f"a different screen answers at {address} now, so the push was not left "
                f"there. This screen is not on the network at the moment, or not where "
                f"this controller can find it. If it is on, open its own panel to see its "
                f"address, and pair it again from there")
    if "http error 409" in t:
        return (f"the screen at {address} already has newer content than this push. Press "
                f"Push again to send it what is here now")
    if "http error" in t:
        return f"the screen at {address} answered an error ({exc_text})"
    # No answer at all. The address is the one the screen had when it was paired — if
    # it still has it, the two devices are on the same network but can't talk to each
    # other, which is what guest Wi-Fi "client isolation" does.
    return (f"could not reach the screen at {address}:{port}. It may be off, or this "
            f"network may be stopping devices from talking to each other (client isolation "
            f"on guest Wi-Fi) — try putting both on the same wired or non-guest network")


def push_with_rediscovery(displays: list, items: list, from_name: str, base_url: str,
                          site_key: str, shuffle: bool = False, browse=None, update=None,
                          rename=None, seq=None, fresh=None) -> list:
    """push_targeted, plus the thing an operator should never have to do by hand:
    a display's address is captured when it is paired and DHCP can move it any time.
    When a push can't reach a display, look for it by its device id - a screen that
    turned away a push meant for another says which screen it is, and the LAN browse
    finds the rest; if it is on a different address, remember the new one and push
    again. Only runs the (3-second) LAN browse when something actually failed and those
    answers didn't place it. Every result carries a plain-language 'error' so the panel
    can say what went wrong, not just who.

    `fresh()`, when given, takes a new snapshot (items, from_name, shuffle, seq) the way
    the caller took this one: a screen that already has a push numbered above this one
    is sent what is here now, under a number above what it has."""
    if seq is None:
        seq = next_seq()
    results = push_targeted(displays, items, from_name, base_url, site_key, shuffle, seq=seq)
    by_id = {d["device_id"]: d for d in displays}
    found = {r["elsewhere"]: (d["address"], d["port"])
             for d, r in zip(displays, results) if r.get("elsewhere") in by_id}
    failed = [d for d, r in zip(displays, results) if not r.get("ok") and not r.get("ahead")]
    if browse is not None and any(d["device_id"] not in found for d in failed):
        try:
            for f in browse(3.0):
                if f.get("device_id") and f.get("address"):
                    found.setdefault(f["device_id"], (f["address"], f.get("port")))
        except Exception:
            pass
    moved = []
    for d in failed:
        address, port = found.get(d["device_id"], (None, None))
        if address and (address, port or d.get("port")) != (d.get("address"), d.get("port")):
            nd = dict(d, address=address, port=port or d.get("port"))
            moved.append(nd)
            if update is not None:
                update(nd)                 # remember where it lives now
    if moved:
        # The same push, and the same number: it is the one these screens missed.
        retry = push_targeted(moved, items, from_name, base_url, site_key, shuffle, seq=seq)
        fixed = {m["device_id"]: r for m, r in zip(moved, retry)}
        results = [fixed.get(d["device_id"], r) for d, r in zip(displays, results)]
        for m in moved:
            by_id[m["device_id"]] = m
    ahead = [by_id[d["device_id"]] for d, r in zip(displays, results) if r.get("ahead")]
    if ahead and fresh is not None:
        items2, from2, shuffle2, seq2 = fresh()
        again = push_targeted(ahead, items2, from_name=from2, base_url=base_url,
                              site_key=site_key, shuffle=shuffle2, seq=seq2)
        # Still ahead means a push newer even than this one got there first: up to date.
        fixed = {a["device_id"]: ({"name": r["name"], "ok": True, "count": r.get("count"),
                                   "display_name": None} if r.get("ahead") else r)
                 for a, r in zip(ahead, again)}
        results = [fixed.get(d["device_id"], r) for d, r in zip(displays, results)]
    for d, r in zip(displays, results):
        if not r.get("ok"):
            cur = by_id[d["device_id"]]
            r["error"] = plain_error(r.get("error", ""), cur.get("address", "?"), cur.get("port", "?"))
    # A display that accepted the push said what it is called NOW. If it was renamed
    # since it was paired, remember that, so this controller lists it - and reports
    # on it - by its real name rather than the one it had on pairing day. `rename`
    # only touches a display that is still paired (pairing.rename_display), and a
    # name that cannot be saved never turns a delivered push into a failed one.
    if rename is not None:
        for d, r in zip(displays, results):
            fresh = r.get("display_name")
            fresh = fresh.strip() if isinstance(fresh, str) else ""
            if r.get("ok") and fresh and fresh != by_id[d["device_id"]].get("name"):
                try:
                    rename(d["device_id"], fresh)
                except Exception as exc:
                    print(f"push: delivered, but could not save the display's new name "
                          f"{fresh!r}: {exc}", file=sys.stderr, flush=True)
    return results


# --- display side -----------------------------------------------------------

def _save(playlist: dict) -> None:
    config.DATA.mkdir(parents=True, exist_ok=True)
    tmp = RECEIVED_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(playlist, indent=2))
    tmp.replace(RECEIVED_PATH)


def _check_order(manifest: dict, me, last_seq) -> None:
    """Is this signed push one for this screen to take, given the newest it has taken
    from its controller (`last_seq`)? Raises Refused if not."""
    to, seq = manifest.get("to"), manifest.get("seq")
    if to is not None and to != me:
        raise Refused("this push was meant for another screen", misaddressed=True)
    if seq is None:
        # Pushes from a controller from before numbering carry none, and are taken as
        # before - until a numbered one has been: then one without is an old push played
        # back. Refused as "not paired", which tells an old controller what fixes it.
        if last_seq is not None:
            raise Refused("this push is older than what the screen has", status=403)
        return
    if not seq_ok(seq):
        raise Refused("this push has no usable number")
    if last_seq is not None and seq <= last_seq:
        raise Refused("this screen already has this push, or a newer one", stale=True, seq=last_seq)


def receive(manifest: dict, signature: str, controller_site_key: str,
            me=None, last_seq=None, take=None) -> bool:
    """Verify the manifest came from our controller, then cache it + its images.

    `me` (this screen's device id) and `take` turn on the check that a push is meant
    for this screen and newer than the last one taken from the controller (`last_seq`):
    one that isn't is refused (Refused) before anything is fetched. `take(seq)` records
    the push's number as the newest taken, just before its playlist replaces the old
    one, and says False if one at least as new was taken meanwhile - so a push that took
    longer to fetch than a newer one doesn't put the older content back."""
    if not manifest or not signature:
        return False
    if not commands.verify(controller_site_key, canon(manifest), signature):
        return False
    if me is not None:
        _check_order(manifest, me, last_seq)

    RECV_ASSETS.mkdir(parents=True, exist_ok=True)
    items = []
    for it in manifest.get("items", []):
        if it.get("type") == "url":
            if not web_url(it.get("url")):
                continue
            entry = {"type": "url", "src": it["url"], "seconds": it["seconds"]}
            if it.get("youtube"):
                entry["youtube"] = True
                entry["sound"] = bool(it.get("sound"))
                entry["cc"] = it.get("cc", True)
            items.append(entry)
        elif it.get("type") == "slideshow":
            srcs = []
            for asset_url in it.get("asset_urls", []):
                if not web_url(asset_url):
                    continue
                name = hashlib.sha256(asset_url.encode()).hexdigest()[:16] + ".img"
                try:
                    _download(asset_url, RECV_ASSETS / name)
                except Exception:
                    continue  # skip a slide we couldn't fetch; keep the rest
                srcs.append(f"/recv-asset/{name}")
            if srcs:
                items.append({"type": "slideshow", "srcs": srcs, "seconds": it["seconds"]})
        elif it.get("type") == "video":
            if not web_url(it.get("url") or it.get("asset_url")):
                continue
            if it.get("url"):
                entry = {"type": "video", "src": it["url"], "seconds": it["seconds"]}
            elif it.get("asset_url"):
                ext = Path(it["asset_url"].split("?")[0]).suffix or ".mp4"
                name = hashlib.sha256(it["asset_url"].encode()).hexdigest()[:16] + ext
                try:
                    _download(it["asset_url"], RECV_ASSETS / name)
                except Exception:
                    continue  # skip a video we couldn't fetch; keep the rest
                entry = {"type": "video", "src": f"/recv-asset/{name}", "seconds": it["seconds"]}
            else:
                continue
            if it.get("sound"):
                entry["sound"] = True
            items.append(entry)
        else:
            if not web_url(it.get("asset_url")):
                continue
            name = hashlib.sha256(it["asset_url"].encode()).hexdigest()[:16] + ".img"
            try:
                urllib.request.urlretrieve(it["asset_url"], str(RECV_ASSETS / name))
            except Exception:
                continue  # skip an image we couldn't fetch; keep the rest playing
            items.append({"type": "image", "src": f"/recv-asset/{name}", "seconds": it["seconds"]})
    with _SAVE_LOCK:
        if take is not None and not take(manifest.get("seq")):
            raise Refused("this screen already has this push, or a newer one", stale=True)
        _save({"items": items, "shuffle": bool(manifest.get("shuffle"))})
    return True


def clear_received() -> None:
    """Forget the pushed playlist + its cached images, so the screen falls back to
    its own local content (or the idle screen). Lets an operator take pushed
    content down from the display itself when the controller is offline."""
    try:
        RECEIVED_PATH.unlink()
    except OSError:
        pass
    shutil.rmtree(RECV_ASSETS, ignore_errors=True)


def _received() -> dict:
    if not RECEIVED_PATH.exists():
        return {}
    try:
        data = json.loads(RECEIVED_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def screen_items():
    """The pushed playlist for this display, or None if nothing was pushed."""
    data = _received()
    if not data:
        return None
    return data.get("items", [])


def screen_shuffle() -> bool:
    """Whether the controller wants its pushed playlist shuffled. A playlist pushed
    before this existed simply has no flag, and plays in order."""
    return bool(_received().get("shuffle"))


def recv_asset_path(name: str) -> Path:
    return RECV_ASSETS / name
