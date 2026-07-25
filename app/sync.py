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
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import commands, config

RECEIVED_PATH = config.DATA / "received.json"
RECV_ASSETS = config.DATA / "recv_assets"


def canon(manifest: dict) -> bytes:
    """Stable byte form both sides sign/verify identically."""
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()


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

def build_manifest(items: list, from_name: str, base_url: str) -> dict:
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
    return {"items": out, "from": from_name}


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


def push_targeted(displays: list, items: list, from_name: str, base_url: str, site_key: str) -> list:
    """Per-display targeting: build each display its own manifest from the items
    aimed at it, sign it, and push. Untargeted items stack onto every display.

    Displays are pushed concurrently, so one slow or offline display can't hold up
    the rest — the whole push takes as long as the slowest single display, not the
    sum of them all. Result order matches the input display order."""
    def _one(d: dict) -> dict:
        name = d.get("name") or d["device_id"][:8]
        mine = items_for_display(items, d["device_id"])
        manifest = build_manifest(mine, from_name, base_url)
        signature = commands.sign(site_key, canon(manifest))
        url = f"http://{d['address']}:{d['port']}/api/playlist"
        try:
            post_json(url, {"manifest": manifest, "signature": signature})
            return {"name": name, "ok": True, "count": len(mine)}
        except Exception as exc:
            return {"name": name, "ok": False, "error": str(exc)}

    if not displays:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(displays))) as pool:
        return list(pool.map(_one, displays))


# --- display side -----------------------------------------------------------

def _save(playlist: dict) -> None:
    config.DATA.mkdir(parents=True, exist_ok=True)
    tmp = RECEIVED_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(playlist, indent=2))
    tmp.replace(RECEIVED_PATH)


def receive(manifest: dict, signature: str, controller_site_key: str) -> bool:
    """Verify the manifest came from our controller, then cache it + its images."""
    if not manifest or not signature:
        return False
    if not commands.verify(controller_site_key, canon(manifest), signature):
        return False

    RECV_ASSETS.mkdir(parents=True, exist_ok=True)
    items = []
    for it in manifest.get("items", []):
        if it.get("type") == "url":
            entry = {"type": "url", "src": it["url"], "seconds": it["seconds"]}
            if it.get("youtube"):
                entry["youtube"] = True
                entry["sound"] = bool(it.get("sound"))
                entry["cc"] = it.get("cc", True)
            items.append(entry)
        elif it.get("type") == "slideshow":
            srcs = []
            for asset_url in it.get("asset_urls", []):
                name = hashlib.sha256(asset_url.encode()).hexdigest()[:16] + ".img"
                try:
                    _download(asset_url, RECV_ASSETS / name)
                except Exception:
                    continue  # skip a slide we couldn't fetch; keep the rest
                srcs.append(f"/recv-asset/{name}")
            if srcs:
                items.append({"type": "slideshow", "srcs": srcs, "seconds": it["seconds"]})
        elif it.get("type") == "video":
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
            name = hashlib.sha256(it["asset_url"].encode()).hexdigest()[:16] + ".img"
            try:
                urllib.request.urlretrieve(it["asset_url"], str(RECV_ASSETS / name))
            except Exception:
                continue  # skip an image we couldn't fetch; keep the rest playing
            items.append({"type": "image", "src": f"/recv-asset/{name}", "seconds": it["seconds"]})
    _save({"items": items})
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


def screen_items():
    """The pushed playlist for this display, or None if nothing was pushed."""
    if not RECEIVED_PATH.exists():
        return None
    try:
        return json.loads(RECEIVED_PATH.read_text()).get("items", [])
    except (json.JSONDecodeError, OSError):
        return None


def recv_asset_path(name: str) -> Path:
    return RECV_ASSETS / name
