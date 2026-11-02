# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
"""Web service (FastAPI).

Routes (by role):
  - GET  /                 first-run setup, or the role's home
  - POST /api/role         set or switch the device role
  - POST /api/name         rename the device and sync the OS hostname
  - GET  /screen           the full-screen page the kiosk browser loads
  - GET  /api/screen-data  the playlist (and any active pairing code) for /screen
  - GET  /asset/{ref}      a cached image asset
  - content:  POST /api/content/url | /api/content/upload | /api/content/remove
  - display pairing:  POST /api/pair/start | /api/pair/cancel | /api/pair/claim
  - controller pairing:  GET /api/discover, POST /api/displays/add | /api/displays/remove

This service listens on the LAN. Login still needs to gate it before release.
"""

import html as _htmllib
import io
import ipaddress
import secrets
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import (
    activity,
    audio,
    auth,
    commands,
    config,
    discovery,
    hostname,
    identity,
    library,
    net,
    pairing,
    promote,
    sessions,
    sync,
    updater,
    wifi,
)
from . import __version__ as APP_VERSION

# Changes every time the app (re)starts — e.g. after an update. The kiosk page
# watches this and reloads itself when it changes, so a new screen.html actually
# reaches the display instead of the browser running whatever it loaded at boot.
_BOOT_ID = str(time.time())

# The startup screen (the address, and a QR code to open it) is for finding a screen
# that has just been switched on. It shows for a minute, and not at all once the screen
# has been on for two: a reload after an update, or the browser restarting, goes
# straight back to the slides.
STARTUP_SCREEN = 60
STARTUP_SCREEN_UNTIL = 120


def _seconds_since_power_on():
    """How long ago this device was switched on - not when the app started, which an
    update or a crash also does. None where the system can't say."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return None


def _startup_left():
    """Seconds the startup screen still has, from now: none once the screen has been on
    a while. None where the system can't say, which leaves it to the page (a minute).
    After a failed update it gets its minute again, because that is where the screen
    says so - until the problem is cleared on the panel."""
    if updater.alert():
        return STARTUP_SCREEN
    on = _seconds_since_power_on()
    return None if on is None else max(0, min(STARTUP_SCREEN, STARTUP_SCREEN_UNTIL - on))


_check_errors: set = set()   # what has already been written to the journal


def _note_check_error(exc: Exception) -> None:
    """One journal line per kind of failure, not one per screen poll."""
    what = f"{type(exc).__name__}: {exc}"
    if what not in _check_errors and len(_check_errors) < 50:
        _check_errors.add(what)
        print(f"screen: could not check the playlist's links ({what})", file=sys.stderr, flush=True)


def _fetched_over_network(item: dict) -> bool:
    """Does showing this item need the internet? The same test as needsNet() on the
    screen: links, decks and YouTube always; a video only when it is a web link."""
    if item.get("type") == "url":
        return True
    return item.get("type") == "video" and str(item.get("src", "")).lower().startswith(("http://", "https://"))


_SETUP_HTML = (Path(__file__).parent / "pages" / "setup.html").read_text(encoding="utf-8")
_SCREEN_HTML = (Path(__file__).parent / "pages" / "screen.html").read_text(encoding="utf-8")
_STATIC_DIR = Path(__file__).parent / "static"  # locally-vendored fonts, etc.
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GB ceiling for any single upload


def create_app() -> FastAPI:
    device_id = identity.get_or_create_device_id()
    _started = time.time()

    def current() -> dict:
        cfg = config.load_config()
        if not cfg.get("name"):
            cfg["name"] = identity.default_name(device_id)
        return cfg

    def _content_back() -> str:
        # After a content change, return to where content lives for this role: a
        # controller keeps it on the /content page, a display on its home page.
        return "/content" if current().get("role") == "controller" else "/"

    def _push_now() -> list:
        """Send the current playlist to every paired display. Safe to run from a
        background task: per-display network failures are captured in the result,
        never raised."""
        displays = pairing.list_displays()
        if not displays:
            return []
        base_url = f"http://{discovery.primary_ip()}:{config.PORT}"
        return sync.push_with_rediscovery(
            displays, library.list_items(), current()["name"],
            base_url, auth.get_or_create_site_key(),
            shuffle=bool(current().get("shuffle")),
            browse=discovery.browse, update=pairing.add_display_record,
            rename=pairing.rename_display,
        )

    def _autopush(background: BackgroundTasks) -> None:
        """After a content change on a controller, push to the displays automatically
        so an edit lands on the screens without anyone clicking 'Push'. It runs after
        the response, so saving the change stays instant. A display never auto-pushes."""
        if current().get("role") == "controller" and pairing.list_displays():
            background.add_task(_push_now)

    def _remeasure_and_push() -> None:
        """Read the length of any Google Slides deck we couldn't size yet, then push
        again if anything changed. Kept off the request path so 'Push' returns fast
        instead of blocking on a headless browser."""
        # Only decks that are due: each attempt is an anonymous load of the deck, which
        # is exactly the traffic Google throttles with a sign-in page. A deck it is
        # refusing must not be hammered on every Push (see library.MEASURE_RETRY).
        changed = False
        for it in library.list_items():
            if library.due_for_measure(it):
                library.measure_slides(it["id"])
                changed = True
        if changed:
            _push_now()

    def _advertise_now(app: FastAPI) -> None:
        # Best-effort LAN advertisement so controllers can discover this device.
        # Discovery must never block or crash the app, so failures are swallowed.
        try:
            cfg = current()
            app.state.zc = discovery.advertise(
                device_id, cfg["name"], cfg.get("role") or "unset", config.PORT
            )
        except Exception:
            app.state.zc = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Replaces the deprecated @app.on_event("startup") and also tears the
        # mDNS advertisement down cleanly on shutdown.
        _advertise_now(app)
        try:
            yield
        finally:
            zc = getattr(app.state, "zc", None)
            if zc is not None:
                try:
                    zc.close()
                except Exception:
                    pass

    app = FastAPI(title="eximaro", lifespan=lifespan)

    # Serve locally-vendored assets (fonts) so the offline device never reaches
    # out to a CDN. Created defensively in case the folder is somehow absent.
    _STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # --- optional admin login gate -----------------------------------------
    # Open by default: with no password set the panel behaves exactly as before.
    # Once an admin sets a password, the human/admin routes require a session.
    # Machine + kiosk routes stay open: the signed pairing/push endpoints, the
    # screen and its data, image assets (a display fetches these during a push),
    # the static files, the QR, and the login page itself.
    _OPEN_EXACT = {"/healthz", "/screen", "/api/screen-data", "/login", "/logout",
                   "/qr.png", "/favicon.ico",
                   # Update progress is not sensitive (like /healthz) and MUST stay
                   # readable across the mid-update restart, which wipes in-memory
                   # sessions — otherwise the update overlay's poll would hang.
                   "/api/update-status"}
    _OPEN_PREFIX = ("/asset/", "/recv-asset/", "/static/", "/api/pair/claim", "/api/playlist")
    # Wi-Fi setup stays open to a phone on this device's OWN setup network: it has no
    # session, and getting onto that network took the password shown on the TV. Reached
    # from the normal network, these are panel pages like any other and need signing in
    # once a password is set - otherwise anyone there could move the screen to another
    # network, or knock it off this one.
    _SETUP_NETWORK_OPEN = {"/wifi-setup", "/wifi-qr.png", "/wifi-open-qr.png",
                           "/api/wifi/connect"}
    # The TV itself shows the two setup QR codes while it hosts that network.
    _SCREEN_OPEN = {"/wifi-qr.png", "/wifi-open-qr.png"}
    _login_fails = {}  # client ip -> (count, window_start): basic brute-force throttle

    def _throttled(ip: str) -> bool:
        rec = _login_fails.get(ip)
        if not rec:
            return False
        count, start = rec
        if time.time() - start > 300:
            _login_fails.pop(ip, None)
            return False
        return count >= 8

    def _note_fail(ip: str) -> None:
        count, start = _login_fails.get(ip, (0, time.time()))
        if time.time() - start > 300:
            count, start = 0, time.time()
        _login_fails[ip] = (count + 1, start)

    def _authed(request: Request) -> bool:
        return sessions.valid(request.cookies.get(sessions.COOKIE_NAME))

    # Captive portal: while this device hosts its own setup network, a joined phone's
    # "do I have internet?" probes reach us — the AP's DNS resolves every name to us
    # and :80 is redirected to the app. Answering an outside-domain probe with a
    # redirect to the setup page is the signal that makes the phone pop it open on its
    # own. Only ever fires while hosting the AP; a normal client never hits it (there's
    # no wildcard DNS or :80 redirect off the AP). hosting_ap() scans interfaces, so
    # cache it briefly since this runs on every request.
    _OWN_HOSTS = {"10.42.0.1", "127.0.0.1", "localhost", ""}
    _ap_cache = {"val": False, "at": 0.0}

    def _hosting_ap_cached() -> bool:
        now = time.monotonic()
        if now - _ap_cache["at"] > 5.0:
            try:
                _ap_cache["val"] = discovery.hosting_ap()
            except Exception:
                _ap_cache["val"] = False
            _ap_cache["at"] = now
        return _ap_cache["val"]

    def _via_setup_network(request: Request) -> bool:
        """Did this request arrive over the setup network this device is hosting? Both
        ends have to fit: the phone has a setup-network address, AND it reached us at the
        setup network's own gateway address. A real network that happens to use the same
        10.42 range never does both."""
        server = (request.scope.get("server") or ("", 0))[0]
        try:
            client = ipaddress.ip_address(request.client.host if request.client else "")
        except ValueError:
            return False
        return (server == wifi.AP_GATEWAY and client in _SETUP_NETWORK
                and _hosting_ap_cached())

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        # Captive-portal redirect comes first: an outside-domain request while we host
        # the AP is a connectivity probe — send it to the setup page. Requests for our
        # own gateway/host fall through and route normally (no loop, no 404 on probes).
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
        if host not in _OWN_HOSTS and not host.endswith(".local") and _hosting_ap_cached():
            return RedirectResponse(wifi.setup_page_url(), status_code=302)
        path = request.url.path
        if path in _OPEN_EXACT or any(path.startswith(p) for p in _OPEN_PREFIX):
            return await call_next(request)
        if path in _SETUP_NETWORK_OPEN and _via_setup_network(request):
            return await call_next(request)
        if path in _SCREEN_OPEN and _from_this_device(request):
            return await call_next(request)
        if not auth.has_credentials():        # no password set → open access
            return await call_next(request)
        if _authed(request):
            return await call_next(request)
        # Browser navigation (any non-API GET) goes to the login page; API calls
        # and form posts get a clean 401 so callers don't mistake HTML for data.
        if request.method == "GET" and not path.startswith("/api/"):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"error": "login required"}, status_code=401)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if not auth.has_credentials() or _authed(request):
            return RedirectResponse("/", status_code=303)
        return _login_page(current())

    @app.post("/login")
    def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
        ip = request.client.host if request.client else "?"
        if _throttled(ip):
            return _login_page(current(), "Too many attempts. Wait a few minutes, then try again.")
        if auth.verify(username.strip(), password):
            _login_fails.pop(ip, None)
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(sessions.COOKIE_NAME, sessions.create(),
                            httponly=True, samesite="lax", max_age=sessions.TTL, path="/")
            activity.log("Signed in to the control panel")
            return resp
        _note_fail(ip)
        activity.log("A sign-in attempt failed")
        return _login_page(current(), "That username or password didn't match.")

    @app.post("/logout")
    def logout(request: Request):
        sessions.destroy(request.cookies.get(sessions.COOKIE_NAME))
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(sessions.COOKIE_NAME, path="/")
        return resp

    @app.post("/api/password/set")
    def password_set(request: Request, username: str = Form(...), password: str = Form(...)):
        # The middleware already gates this: setting the FIRST password is open
        # (like first-run setup); CHANGING one requires a live session. So the
        # caller is authorized by the time we get here.
        if len(password) < 6:
            return JSONResponse({"error": "Password must be at least 6 characters."}, status_code=400)
        first = not auth.has_credentials()
        auth.set_credentials(username.strip() or "admin", password)
        if not first:
            sessions.clear_all()  # a password change evicts every other session
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(sessions.COOKIE_NAME, sessions.create(),
                        httponly=True, samesite="lax", max_age=sessions.TTL, path="/")
        activity.log("Set the control-panel password" if first else "Changed the control-panel password")
        return resp

    @app.get("/password/clear", response_class=HTMLResponse)
    def password_clear_confirm():
        cfg = current()
        return _page("Remove the password", cfg.get("role") or "display", cfg, _password_clear_body(cfg))

    @app.post("/api/password/clear")
    def password_clear(request: Request, confirm: str = Form(default="")):
        # Reachable only with a live session (the middleware gates it).
        if confirm != "yes":
            return RedirectResponse("/password/clear", status_code=303)
        auth.clear_credentials()
        sessions.clear_all()
        activity.log("Removed the control-panel password — the panel is open again")
        return RedirectResponse("/", status_code=303)

    def _readvertise() -> None:
        # Role or name changed — re-publish so discovery reflects the current
        # state instead of the value captured at boot.
        zc = getattr(app.state, "zc", None)
        if zc is not None:
            try:
                zc.close()
            except Exception:
                pass
        _advertise_now(app)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "device_id": device_id, "build": config.BUILD_TAG,
                "version": config.VERSION}

    @app.get("/", response_class=HTMLResponse)
    def home():
        cfg = current()
        role = cfg.get("role")
        if role not in config.VALID_ROLES:
            return _splash(cfg)
        if role == "controller":
            return _control_home(cfg)
        return _display_home(cfg)

    @app.get("/content", response_class=HTMLResponse)
    def content_page():
        cfg = current()
        role = cfg.get("role")
        if role not in config.VALID_ROLES:
            return _splash(cfg)
        # Only a controller has a separate Content page; a display keeps its content
        # on its own home (with the pushed-content note), so send it there instead.
        if role != "controller":
            return RedirectResponse("/", status_code=303)
        return _content_page(cfg, device_id)

    @app.get("/health", response_class=HTMLResponse)
    def health():
        cfg = current()
        role = cfg.get("role") or "display"
        return _page("Health", role, cfg, _health_body(cfg, role, device_id, _started), active="health")

    @app.get("/role", response_class=HTMLResponse)
    def role_confirm(role: str = ""):
        cfg = current()
        now = cfg.get("role")
        if role not in config.VALID_ROLES or now not in config.VALID_ROLES or role == now:
            return RedirectResponse("/", status_code=303)
        return _page("Switch role", now, cfg, _role_switch_body(cfg, role, device_id))

    @app.post("/api/role")
    def set_role(role: str = Form(...), confirm: str = Form(default="")):
        if role not in config.VALID_ROLES:
            return JSONResponse({"error": "invalid role"}, status_code=400)
        cfg = current()
        now = cfg.get("role")
        if role == now:
            return RedirectResponse("/", status_code=303)
        # The very first choice, on the first-run setup page, is its own explanation.
        # Changing a role that is already set goes through the confirmation screen.
        if now in config.VALID_ROLES and confirm != "yes":
            return RedirectResponse(f"/role?role={role}", status_code=303)
        cfg["role"] = role
        config.save_config(cfg)
        _readvertise()
        activity.log(f"Switched this device's role to {role}")
        # A new controller needs LibreOffice/poppler to convert PowerPoint. The
        # app is sandboxed, so ask the privileged helper to install them.
        if role == "controller" and not promote.has_conversion_tools():
            promote.request_promotion()
            activity.log("Requested install of PowerPoint conversion packages")
        return RedirectResponse("/", status_code=303)

    @app.get("/display-here", response_class=HTMLResponse)
    def display_here_confirm():
        cfg = current()
        if cfg.get("role") != "controller" or not cfg.get("display_content", True):
            return RedirectResponse("/", status_code=303)
        return _page("This screen", "controller", cfg, _display_here_off_body(cfg, device_id))

    @app.post("/api/display-here")
    def display_here(show: str = Form(default=""), confirm: str = Form(default="")):
        # Whether a controller mirrors the playlist on its own attached screen.
        # Off makes it a pure management box (blank/idle screen). Displays ignore it.
        cfg = current()
        on = show == "on"
        if on == cfg.get("display_content", True):
            return RedirectResponse("/", status_code=303)
        # Turning it on brings the playlist back at once. Turning it off blanks a TV,
        # so on a controller that goes through the confirmation screen first.
        if not on and cfg.get("role") == "controller" and confirm != "yes":
            return RedirectResponse("/display-here", status_code=303)
        cfg["display_content"] = on
        config.save_config(cfg)
        activity.log("Content on this screen: " + ("on" if cfg["display_content"] else "off"))
        return RedirectResponse("/", status_code=303)

    @app.post("/api/name")
    def set_name(background: BackgroundTasks, name: str = Form(...)):
        cfg = current()
        cfg["name"] = name.strip() or cfg["name"]
        config.save_config(cfg)
        if cfg.get("sync_hostname", True):
            hostname.apply_hostname(cfg["name"])
        _readvertise()
        activity.log(f"Renamed this device to {cfg['name']}")
        # A controller's screens show its name: push now so they learn the new one
        # straight away, not at the next content change. (No-op on a display.)
        _autopush(background)
        return RedirectResponse("/", status_code=303)

    # --- the screen the kiosk shows -----------------------------------------

    @app.get("/screen", response_class=HTMLResponse)
    def screen():
        return HTMLResponse(_SCREEN_HTML)

    @app.get("/api/screen-data")
    def screen_data(request: Request):
        cfg = current()
        is_controller = cfg.get("role") == "controller"
        # A controller can mirror the playlist on its own screen too (default), or be
        # a pure management box (toggle off in Settings). A display always shows.
        show_here = not is_controller or cfg.get("display_content", True)
        # A controller PUSHES content; it never plays a pushed playlist (that's the
        # display's job), so it always uses its own library — a stray received.json
        # can't blank its screen.
        pushed = None if is_controller else sync.screen_items()
        # Pushed content plays the way the controller asked, including its shuffle —
        # that setting belongs to the playlist, not to the screen showing it.
        shuffle = bool(cfg.get("shuffle"))
        if pushed is not None:
            # Only a web link may become a page on the screen: a javascript: one would
            # run inside the screen's own page. New pushes are checked on arrival; this
            # also covers a playlist that arrived before that check existed.
            items = [i for i in pushed if i.get("type") != "url" or sync.web_url(i.get("src"))]
            shuffle = sync.screen_shuffle()
        elif show_here:
            items = []
            for item in sync.items_for_display(library.list_items(), device_id):
                if item["type"] == "url":
                    entry = {"type": "url", "src": item["ref"], "seconds": item["seconds"]}
                    if item.get("youtube"):
                        entry["youtube"] = True  # play it in full; the screen times it by its end
                        entry["sound"] = bool(item.get("sound"))
                        entry["cc"] = item.get("cc", True)
                    items.append(entry)
                elif item["type"] == "slideshow":
                    items.append({"type": "slideshow", "seconds": item["seconds"],
                                  "srcs": [f"/asset/{r}" for r in item.get("refs", [])]})
                elif item["type"] == "video":
                    src = item["ref"] if item["ref"].startswith("http") else f"/asset/{item['ref']}"
                    entry = {"type": "video", "src": src, "seconds": item["seconds"]}
                    if item.get("sound"):
                        entry["sound"] = True
                    items.append(entry)
                else:
                    items.append({"type": "image", "src": f"/asset/{item['ref']}", "seconds": item["seconds"]})
        else:
            items = []   # a controller told not to show content on its own screen
        labeled = discovery.labeled_ips()
        ap_up = any(a["ip"].startswith(discovery.AP_SUBNET) for a in labeled)
        addrs = [a for a in labeled if not a["ip"].startswith(discovery.AP_SUBNET)]
        if not addrs and not ap_up:  # no iproute2 / unusual host: fall back
            addrs = [{"label": "Address", "ip": ip} for ip in discovery.lan_ips()]
        # No real connection but hosting the setup network -> the screen shows the
        # join QR + these creds instead of an address.
        setup = None
        if ap_up and not addrs:
            creds = wifi.ap_credentials()
            setup = {"ssid": creds["ssid"], "password": creds["password"]}
        primary = addrs[0]["ip"] if addrs else None
        # Whether each item fetched over the internet can be shown now. A cross-origin
        # iframe never reports that it failed, so without this the screen cannot tell a
        # loaded deck from Chromium's offline error page — and paints the error page onto
        # a public sign. Each item is judged on its own, so one deck Google is refusing
        # never holds back the rest. Costs nothing on an all-local playlist: no remote
        # hosts means no lookup at all, and with no network address there is nothing to
        # look up.
        remote = [i.get("src", "") for i in items if _fetched_over_network(i)]
        since_on = _seconds_since_power_on()
        starting = since_on is not None and since_on < net.STARTUP_GRACE
        try:
            blocked = (net.problems(remote, online=discovery.has_address(), starting=starting)
                       if remote else {})
        except Exception as exc:
            # A check that went wrong must never cost the screen its playlist: hold back
            # what needs the network, play the rest, and say so once in the journal.
            _note_check_error(exc)
            blocked = {u: "couldn't check this link" for u in remote if isinstance(u, str)}
        problem = next(iter(blocked.values()), "")
        return {
            "items": items,
            # The code proves someone is standing at this screen, so only the screen
            # itself gets it - this route is open to the whole network.
            "pairing_code": pairing.current_code() if _from_this_device(request) else None,
            "shuffle": shuffle,
            # The items that can't be shown yet, each with its reason; the screen skips
            # those and plays the rest. When none of them can be shown, it waits.
            "blocked": blocked,
            "content_reachable": not blocked,
            # The first reason, so the waiting card can say "Google is asking viewers to
            # sign in" rather than blaming the network.
            "content_note": problem,
            # Shown ONLY on the boot splash (never over playing content): a failed
            # update should be visible to whoever walks past a rebooted screen.
            "update_alert": updater.alert(),
            # Where to reach this device: every interface, labeled, Wi-Fi first, so a
            # wired + Wi-Fi box shows both and you use whichever network you're on.
            # connect_url (and the QR) is the first one; null means no network yet.
            "connect_url": f"http://{primary}:{config.PORT}" if primary else None,
            "ips": [a["ip"] for a in addrs],
            "addresses": [{"label": a["label"], "url": f"http://{a['ip']}:{config.PORT}"}
                          for a in addrs],
            "wifi_setup": setup,
            "boot": _BOOT_ID,
            # Read after the checks above, which can take a few seconds just after a boot.
            "startup_left": _startup_left(),
        }

    @app.get("/recv-asset/{name}")
    def recv_asset(name: str):
        path = sync.recv_asset_path(name)
        if "/" in name or "\\" in name or not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(path)

    @app.get("/asset/{ref}")
    def asset(ref: str):
        path = library.asset_path(ref)
        if "/" in ref or "\\" in ref or not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(path)

    @app.get("/qr.png")
    def qr_png():
        """A QR code for this device's address, shown on the idle/setup screen so
        someone can open the control panel by scanning instead of typing an IP.
        The encoded address is always this device's own — never client input.
        Any failure (missing qrcode/Pillow, render error) degrades to 404 so the
        screen simply hides the QR rather than erroring."""
        try:
            import qrcode  # local import: a missing optional dep must not block boot
            url = f"http://{discovery.primary_ip()}:{config.PORT}"
            img = qrcode.make(url)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return Response(content=buf.getvalue(), media_type="image/png")
        except Exception:
            return Response(status_code=404)

    # --- WiFi setup ---------------------------------------------------------

    @app.get("/wifi-qr.png")
    def wifi_qr_png():
        """A QR that joins a phone to the setup network (encodes the network name +
        password, never client input). 404 if QR rendering isn't available."""
        try:
            import qrcode
            img = qrcode.make(wifi.join_qr_payload())
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return Response(content=buf.getvalue(), media_type="image/png")
        except Exception:
            return Response(status_code=404)

    @app.get("/wifi-open-qr.png")
    def wifi_open_qr_png():
        """A QR that opens the setup page once a phone has joined the setup network,
        so nobody has to type the address. Encodes this device's fixed AP-gateway
        URL (never client input). 404 if QR rendering isn't available."""
        try:
            import qrcode
            img = qrcode.make(wifi.setup_page_url())
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return Response(content=buf.getvalue(), media_type="image/png")
        except Exception:
            return Response(status_code=404)

    def _wifi_setup_html(creds, st, nets, on_setup_network=False):
        options = "".join(f'<option value="{_esc(n["ssid"])}">' for n in nets if n.get("ssid"))
        if st.get("ssid") and not st.get("ap_active"):
            banner = f'<p class="ok">&#10003; Connected to <b>{_esc(st["ssid"])}</b>.</p>'
        elif st.get("ap_active"):
            banner = (f'<p>This screen is hosting <b>{_esc(creds["ssid"])}</b> '
                      f'&middot; password <b>{_esc(creds["password"])}</b>. Scan to join:</p>'
                      f'<img src="/wifi-qr.png" alt="Join QR" width="200" height="200">')
        else:
            banner = '<p>Enter a WiFi network below and this device will connect to it.</p>'
        # A fixed sentence, never the helper's own words: its error text can carry what
        # was typed (an older helper put the whole join command, Wi-Fi password and
        # all, into it), and this page can be open to anyone on the network.
        err = ('<p class="err">Couldn&rsquo;t connect to that network. Check its name '
               'and password, then try again.</p>') if st.get("error") else ""
        # On the setup network, say what Connect does before it happens: the network the
        # phone is on goes away while the screen joins, which looks like a failure if
        # nobody said so.
        next_html = ('<p class="note">When you tap Connect, this setup network goes away '
                     'while the screen joins yours, and your phone drops off it. If the screen '
                     'can&rsquo;t join, it normally brings the setup network back so you can try '
                     'again. If that hasn&rsquo;t happened after a few minutes, connect the screen '
                     'to your router with a network cable to finish.</p>') if on_setup_network else ""
        # Only offer to take the setup network down when one is running, and only from the
        # normal network: from a phone ON the setup network it would just cut that phone off.
        stop_html = ('<form method="post" action="/api/wifi/stop">'
                     '<button class="stop">Stop setup network</button></form>'
                     if st.get("ap_active") and not on_setup_network else "")
        return _wifi_shell("WiFi setup", f"""<h1>Connect this screen to WiFi</h1>
{banner}{err}
<h2>Share your WiFi</h2>
<form method="post" action="/api/wifi/connect">
  <label>Network name</label>
  <input name="ssid" list="nets" placeholder="Your WiFi name" autocapitalize="off" required>
  <datalist id="nets">{options}</datalist>
  <label>Password</label>
  <input name="password" type="password" placeholder="WiFi password">
  <button type="submit">Connect</button>
</form>
{next_html}
{stop_html}""")

    @app.get("/wifi-setup", response_class=HTMLResponse)
    def wifi_setup(request: Request):
        return HTMLResponse(_wifi_setup_html(wifi.ap_credentials(), wifi.status(), wifi.scan(),
                                             on_setup_network=_via_setup_network(request)))

    @app.post("/api/wifi/start")
    def wifi_start():
        # Explicit operator action: host the setup AP AND tell the watcher to leave it
        # up, so it isn't torn straight back down on a device that has a saved network.
        wifi.start_manual_setup()
        activity.log("Started the WiFi setup network")
        return RedirectResponse("/wifi-setup", status_code=303)

    @app.post("/api/wifi/connect")
    def wifi_connect(request: Request, ssid: str = Form(...), password: str = Form(""),
                     confirm: str = Form("")):
        ssid = ssid.strip()
        if not ssid:
            return RedirectResponse("/wifi-setup", status_code=303)
        # Joining from the screen's own setup network IS the setup: there is no other
        # network to lose. From the normal network it can take the screen off the very
        # network this page is open on, so that gets a screen first - sent as this POST's
        # answer, never a redirect, so the password never ends up in an address.
        if confirm != "yes" and not _via_setup_network(request):
            here = (request.scope.get("server") or ("", 0))[0]
            return _wifi_move_screen(current(), ssid, password, here)
        wifi.request_connect(ssid, password)
        activity.log("Joining a WiFi network", ssid)
        return RedirectResponse("/wifi-setup", status_code=303)

    @app.post("/api/wifi/stop")
    def wifi_stop():
        wifi.request_stop()
        return RedirectResponse("/wifi-setup", status_code=303)

    # --- content ------------------------------------------------------------

    @app.post("/api/content/url")
    def add_url(background: BackgroundTasks, url: str = Form(...),
                seconds: int = Form(library.DEFAULT_URL_SECONDS)):
        item = library.add_url(url, seconds)
        # Sizing a Google Slides deck launches a headless browser (seconds, sometimes
        # tens of them). Do it AFTER the response so saving a link is instant — the
        # item just shows "length not read yet" with a Re-check button until it lands,
        # then updates itself. A non-Slides link makes measure_slides a no-op anyway.
        background.add_task(library.measure_slides, item["id"])
        _autopush(background)   # measure runs first, then the push carries its timing
        activity.log("Added a link to the playlist", url)
        return RedirectResponse(_content_back(), status_code=303)

    @app.get("/api/content/measuring")
    def content_measuring():
        """How many decks are still being timed. The Content page polls this after a
        link is added and refreshes itself when the count reaches zero, so the row
        turns from "reading the deck" into "auto" without anyone clicking anything."""
        return {"pending": sum(1 for it in library.list_items() if library.measuring(it))}

    @app.post("/api/content/upload")
    async def add_upload(background: BackgroundTasks, file: UploadFile,
                         seconds: int = Form(library.DEFAULT_IMAGE_SECONDS)):
        name = file.filename or "upload"
        low = name.lower()
        # Stream the upload to a temp file in chunks so a large video is never held
        # whole in memory (a small controller would run out of RAM). Conversion +
        # disk work runs off the event loop.
        config.WORK.mkdir(parents=True, exist_ok=True)
        tmp = config.WORK / ("upload_" + secrets.token_hex(8) + (Path(name).suffix.lower() or ".bin"))
        try:
            total = 0
            with open(tmp, "wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise ValueError("file too large")
                    out.write(chunk)
            if low.endswith((".pptx", ".ppt")):
                item = await run_in_threadpool(library.add_pptx, name, tmp.read_bytes(), seconds)
                activity.log(f"Added a PowerPoint ({item.get('slides', 0)} slide(s))", name)
            elif low.endswith(library.VIDEO_EXTS):
                await run_in_threadpool(library.add_video_path, name, str(tmp))
                tmp = None  # moved into assets; nothing to clean up
                activity.log("Added a video to the playlist", name)
            else:
                await run_in_threadpool(library.add_image, name, tmp.read_bytes(), seconds)
                activity.log("Added an image to the playlist", name)
        except ValueError:
            return JSONResponse({"error": "That file is too large (1 GB max)."}, status_code=413)
        except RuntimeError as exc:
            activity.log("An upload could not be processed", str(exc))
            return JSONResponse({"error": str(exc)}, status_code=400)
        finally:
            if tmp is not None:
                try:
                    Path(tmp).unlink()
                except OSError:
                    pass
        # The file is fully processed by now (conversion is awaited above), so the
        # pushed manifest references content that actually exists.
        _autopush(background)
        return RedirectResponse(_content_back(), status_code=303)

    @app.get("/content/remove", response_class=HTMLResponse)
    def remove_content_confirm(item_id: str = ""):
        cfg = current()
        role = cfg.get("role") or "display"
        return _page("Remove from the playlist", role, cfg, _remove_item_body(cfg, item_id, device_id),
                     active="content" if role == "controller" else "home")

    @app.post("/api/content/remove")
    def remove_content(background: BackgroundTasks, item_id: str = Form(...),
                       confirm: str = Form(default="")):
        # Only the confirmation screen's explicit yes removes anything (see _confirm_card).
        if confirm != "yes":
            return RedirectResponse(f"/content/remove?item_id={quote(item_id)}", status_code=303)
        gone = next((i for i in library.list_items() if i["id"] == item_id), None)
        if gone is None:                   # already removed: a second tap, an old tab
            return RedirectResponse(_content_back(), status_code=303)
        library.remove(item_id)
        _autopush(background)
        activity.log("Removed an item from the playlist", gone.get("name") or "")
        return RedirectResponse(_content_back(), status_code=303)

    @app.post("/api/content/seconds")
    def content_seconds(background: BackgroundTasks, item_id: str = Form(...), seconds: int = Form(...)):
        library.set_seconds(item_id, seconds)
        _autopush(background)
        return RedirectResponse(_content_back(), status_code=303)

    @app.post("/api/content/sound")
    def content_sound(background: BackgroundTasks, item_id: str = Form(...), sound: str = Form(default="")):
        # The checkbox posts "on" when ticked, nothing when not — so its presence is
        # the value. A muted screen is the norm, so sound is off unless asked for.
        library.set_sound(item_id, sound == "on")
        _autopush(background)
        return RedirectResponse(_content_back(), status_code=303)

    @app.post("/api/content/cc")
    def content_cc(background: BackgroundTasks, item_id: str = Form(...), cc: str = Form(default="")):
        library.set_cc(item_id, cc == "on")
        _autopush(background)
        return RedirectResponse(_content_back(), status_code=303)

    @app.post("/api/content/remeasure")
    def content_remeasure(background: BackgroundTasks, item_id: str = Form(...)):
        # Read a Google Slides deck's length now (it needs to be online). Lets a deck
        # that was added on a flaky connection size itself without re-adding it.
        item = library.measure_slides(item_id)
        if item.get("slides"):
            activity.log(f"Read a Google Slides deck — {item['slides']} slide(s)")
        _autopush(background)
        return RedirectResponse(_content_back(), status_code=303)

    @app.post("/api/content/move")
    def content_move(background: BackgroundTasks, item_id: str = Form(...), direction: str = Form(...)):
        if direction in ("up", "down"):
            library.move(item_id, direction)
        _autopush(background)
        return RedirectResponse(_content_back(), status_code=303)

    @app.post("/api/content/targets")
    def content_targets(background: BackgroundTasks, item_id: str = Form(...),
                        display: list[str] = Form(default=[])):
        # No displays checked = show on every screen; otherwise just the chosen ones.
        library.set_targets(item_id, display)
        _autopush(background)
        activity.log("Changed which screens an item shows on")
        return RedirectResponse(_content_back(), status_code=303)

    @app.post("/api/playback")
    def set_playback(background: BackgroundTasks, shuffle: str = Form(default="")):
        cfg = current()
        cfg["shuffle"] = bool(shuffle)
        config.save_config(cfg)
        # Shuffle sits with the playlist, so it has to travel like the rest of it —
        # the Content page promises changes reach the displays on their own.
        _autopush(background)
        activity.log("Shuffle turned " + ("on" if cfg["shuffle"] else "off"))
        return RedirectResponse(_content_back(), status_code=303)

    @app.get("/update", response_class=HTMLResponse)
    def update_confirm():
        cfg = current()
        return _page("Update", cfg.get("role") or "display", cfg, _update_body(cfg))

    @app.post("/api/update")
    def do_update(confirm: str = Form(default="")):
        if confirm != "yes":
            return RedirectResponse("/update", status_code=303)
        if _update_running():               # one is already under way
            return RedirectResponse("/", status_code=303)
        updater.request_update()
        activity.log("Started a software update")
        return RedirectResponse("/", status_code=303)

    @app.post("/api/audio")
    def set_audio(output: str = Form(...)):
        # The panel posts an output the device reported, or "auto" to go back to
        # picking HDMI itself. A root path unit does the switch — the app can't
        # write /etc. Anything the device didn't report is refused rather than
        # written, so a stale page can't pin something unusable.
        if not audio.request_output(output):
            return RedirectResponse("/", status_code=303)
        activity.log("Set the audio output back to automatic" if output == audio.AUTO
                     else "Changed the audio output")
        return RedirectResponse("/", status_code=303)

    @app.post("/api/audio/refresh")
    def refresh_audio():
        # Re-scan the hardware (e.g. a soundbar was just plugged in).
        audio.refresh()
        return RedirectResponse("/", status_code=303)

    @app.post("/api/update/ack")
    def update_ack():
        # Operator has seen the failed-update banner; stop showing it.
        updater.acknowledge()
        activity.log("Dismissed an update-failure notice")
        return RedirectResponse("/", status_code=303)

    @app.get("/api/update-status")
    def update_status():
        # The update UI polls this so it can react to the ACTUAL outcome (done /
        # failed / rolled_back) instead of guessing from /healthz — a failed update
        # never restarts the app, so there's no restart to detect. `in_progress` is
        # true only while a request file is still sitting unhandled.
        return {"in_progress": updater.in_progress(), **updater.status()}

    @app.get("/promote", response_class=HTMLResponse)
    def promote_confirm():
        cfg = current()
        return _page("PowerPoint support", cfg.get("role") or "display", cfg, _promote_body(cfg))

    @app.post("/api/promote")
    def promote_now(confirm: str = Form(default="")):
        # Manual (re)trigger of the conversion-package install from the UI.
        if confirm != "yes":
            return RedirectResponse("/promote", status_code=303)
        if not promote.has_conversion_tools():
            promote.request_promotion()
            activity.log("Requested install of PowerPoint conversion packages")
        return RedirectResponse("/", status_code=303)

    # --- pairing: display side ----------------------------------------------

    @app.get("/pair/start", response_class=HTMLResponse)
    def pair_start_confirm():
        cfg = current()
        if not pairing.is_claimed():
            return RedirectResponse("/", status_code=303)
        return _page("Re-pair this screen", cfg.get("role") or "display", cfg,
                     _repair_body(cfg))

    @app.post("/api/pair/start")
    def pair_start(confirm: str = Form(default="")):
        # An unpaired screen shows its code at once: that is how pairing starts, and
        # "Cancel pairing" undoes it. A paired one covers its content with the code and
        # can be taken over, so that goes through the confirmation screen first.
        claimed = pairing.is_claimed()
        if claimed and confirm != "yes":
            return RedirectResponse("/pair/start", status_code=303)
        pairing.start_pairing()
        if claimed:
            activity.log("Showed a pairing code so a different controller can take over this screen")
        return RedirectResponse("/", status_code=303)

    @app.post("/api/pair/cancel")
    def pair_cancel():
        pairing.cancel_pairing()
        return RedirectResponse("/", status_code=303)

    @app.get("/received/clear", response_class=HTMLResponse)
    def received_clear_confirm():
        cfg = current()
        return _page("Remove pushed content", cfg.get("role") or "display", cfg,
                     _clear_pushed_body(cfg, device_id))

    @app.post("/api/received/clear")
    def received_clear(confirm: str = Form(default="")):
        # Take down content a controller pushed — usable from the display itself
        # even when the controller is offline. Falls back to local content / idle.
        if confirm != "yes":
            return RedirectResponse("/received/clear", status_code=303)
        sync.clear_received()
        activity.log("Removed content pushed from the controller")
        return RedirectResponse("/", status_code=303)

    @app.get("/pair/forget", response_class=HTMLResponse)
    def pair_forget_confirm():
        """The "are you sure?" screen before a display unpairs. See _unpair_confirm_body."""
        cfg = current()
        return _page("Unpair this screen", cfg.get("role") or "display", cfg, _unpair_confirm_body())

    @app.post("/api/pair/forget")
    def pair_forget(confirm: str = Form(default="")):
        # Unpairing only ever happens from the confirmation screen's explicit "yes".
        # Anything else - an old cached page, a stray click, a bookmarked form - lands
        # on the explanation instead of silently cutting the screen off its controller.
        if confirm != "yes":
            return RedirectResponse("/pair/forget", status_code=303)
        pairing.forget_controller()
        sync.clear_received()
        activity.log("Unpaired from the controller and cleared its pushed content")
        return RedirectResponse("/", status_code=303)

    @app.post("/api/pair/claim")
    async def pair_claim(request: Request):
        body = await request.json()
        try:
            result = pairing.claim(
                body["code"], body["controller"], body["sealed_site_key"]
            )
        except (KeyError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception:
            return JSONResponse({"error": "claim failed"}, status_code=400)
        result["name"] = current()["name"]
        activity.log("Paired with a controller", body.get("controller", {}).get("name", ""))
        return result

    @app.post("/api/playlist")
    async def receive_playlist(request: Request):
        controller = pairing.get_controller()
        if not controller:
            return JSONResponse({"error": "not paired"}, status_code=403)
        body = await request.json()
        # receive() verifies the signature then downloads assets; run it off the
        # event loop so a slow or large push can never block the kiosk web server.
        ok = await run_in_threadpool(
            sync.receive, body.get("manifest"), body.get("signature"), controller["site_key"]
        )
        if not ok:
            return JSONResponse({"error": "rejected"}, status_code=403)
        # The push is verified, so what it says about its sender can be trusted: keep
        # the name and address this screen shows for its controller current, and
        # answer with this screen's own current name so the controller can do the
        # same the other way round. Names always follow renames, in both directions.
        # The content is already in place, so a name that can't be saved is reported
        # to the journal, never turned into a failed push.
        manifest = body.get("manifest")
        try:
            await run_in_threadpool(
                pairing.refresh_controller,
                name=manifest.get("from") if isinstance(manifest, dict) else "",
                address=request.client.host if request.client else "")
        except Exception as exc:
            print(f"push: delivered, but could not save the controller's current name "
                  f"and address: {exc}", file=sys.stderr, flush=True)
        return {"ok": True, "name": current()["name"]}

    # --- pairing: controller side -------------------------------------------

    @app.get("/api/discover")
    def discover():
        try:
            devices = discovery.browse(timeout=3.0)
        except Exception:
            devices = []
        paired = {d["device_id"] for d in pairing.list_displays()}
        out = [d for d in devices if d.get("device_id") and d["device_id"] != device_id]
        for d in out:
            d["paired"] = d["device_id"] in paired
        return {"devices": out}

    @app.post("/api/displays/add")
    def displays_add(
        background: BackgroundTasks,
        address: str = Form(...),
        code: str = Form(...),
        port: int = Form(8080),
    ):
        cfg = current()
        controller_meta = {
            "device_id": device_id,
            "name": cfg["name"],
            "address": discovery.primary_ip(),
        }
        try:
            record = pairing.claim_display(address, port, code, controller_meta)
        except Exception as exc:
            activity.log("A pairing attempt failed", str(exc))
            return JSONResponse({"error": f"Could not pair: {exc}"}, status_code=400)
        activity.log("Paired a display", record.get("name") or address)
        # Send it the playlist straight away, so a freshly paired screen starts
        # playing instead of sitting blank until the next content change.
        _autopush(background)
        return RedirectResponse("/", status_code=303)

    @app.get("/displays/remove", response_class=HTMLResponse)
    def displays_remove_confirm(request: Request):
        cfg = current()
        return _page("Unpair a display", cfg.get("role") or "display", cfg,
                     _remove_display_body(cfg, request.query_params.get("device_id", ""), device_id))

    @app.post("/api/displays/remove")
    def displays_remove(device: str = Form(..., alias="device_id"), confirm: str = Form(default="")):
        if confirm != "yes":
            return RedirectResponse(f"/displays/remove?device_id={quote(device)}", status_code=303)
        gone = next((d for d in pairing.list_displays() if d["device_id"] == device), None)
        if gone is None:                   # already unpaired
            return RedirectResponse("/", status_code=303)
        pairing.remove_display(device)
        activity.log("Unpaired a display", gone.get("name") or "")
        return RedirectResponse("/", status_code=303)

    @app.post("/api/push")
    def push(background: BackgroundTasks):
        displays = pairing.list_displays()
        if not displays:
            return {"results": [], "message": "No displays paired yet."}
        # Push now with whatever timing we have, so this returns immediately instead
        # of blocking on a headless browser. Any Google Slides deck we couldn't size
        # yet gets measured + re-pushed in the background.
        results = _push_now()
        ok = sum(1 for r in results if r.get("ok"))
        activity.log(f"Pushed content to {ok} of {len(results)} display(s)")
        background.add_task(_remeasure_and_push)
        return {"results": results}

    return app


# --- built-in HTML pages (styled to match pages/setup.html design language) ---

def _esc(text) -> str:
    return _htmllib.escape(str(text))


# Tokens + fonts are vendored locally (app/static) so the offline device never
# depends on a CDN. Licensing: app/static/fonts/OFL.txt.
_FONTS_HEAD = (
    '<link rel="icon" href="/static/brand/eximaro-icon.svg">'
    '<link rel="stylesheet" href="/static/tokens.css">'
    '<link rel="stylesheet" href="/static/fonts/fonts.css">'
)

# Apply the operator's saved light/dark choice before the page paints (no flash).
# No saved choice ("auto") leaves it to the device's own prefers-color-scheme.
_THEME_HEAD = (
    '<script>(function(){try{var t=localStorage.getItem("eximaro-theme");'
    'if(t==="dark"||t==="light")document.documentElement.setAttribute("data-theme",t);}'
    "catch(e){}})();</script>"
)

# Big stylesheet kept as a plain (non-f) string so CSS braces need no escaping.
_CSS = """
  *{box-sizing:border-box}
  html,body{margin:0}
  body{
    background:var(--bg); color:var(--ink);
    font:16px/1.55 "Hanken Grotesk",system-ui,sans-serif;
    min-height:100vh; padding:24px;
    -webkit-font-smoothing:antialiased;
  }
  .stage{width:100%;max-width:720px;margin:0 auto}
  a{color:var(--accent-text);text-decoration:none}
  a:hover{text-decoration:underline}

  /* header — the eximaro wordmark rendered as live text, the x in sky */
  .top{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}
  .brand{font-family:"Sora",sans-serif;font-weight:600;font-size:1.15rem;
    letter-spacing:-.01em;color:var(--ink)}
  /* The wordmark's accent letter reuses class "x"; neutralize the remove-button
     .x styling (background/border/padding) so it's just a sky-colored letter. */
  .brand .x{color:var(--accent);background:none;border:0;padding:0;border-radius:0;font:inherit}
  .top .spacer{flex:1}
  .name-chip{font:.8rem ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);
    background:var(--surface);border:1px solid var(--line);
    padding:6px 10px;border-radius:9px;white-space:nowrap}
  .badge{font-size:.66rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
    padding:5px 11px;border-radius:var(--r-pill);
    color:var(--accent-text);background:var(--accent-soft)}

  /* nav pill */
  .nav{display:flex;gap:2px;background:var(--surface);border:1px solid var(--line);
    border-radius:var(--r-pill);padding:3px;margin:0 0 20px;width:max-content;
    max-width:100%;flex-wrap:wrap}
  .nav a{font-size:.8rem;font-weight:600;color:var(--muted);text-decoration:none;
    padding:6px 13px;border-radius:var(--r-pill);cursor:pointer;white-space:nowrap}
  .nav a:hover{color:var(--ink);text-decoration:none}
  .nav a.on{background:var(--ink);color:var(--solid-fg)}

  /* page intro */
  .eyebrow{font-size:.72rem;font-weight:600;letter-spacing:.12em;text-transform:uppercase;
    color:var(--accent-text);margin:0 0 8px}
  h1{font-family:"Sora",sans-serif;font-weight:600;
    font-size:clamp(1.7rem,4vw,2.2rem);line-height:1.08;letter-spacing:-.02em;margin:0 0 6px}
  .lead{color:var(--muted);margin:0 0 22px}

  /* cards */
  .card{background:var(--surface);border:1px solid var(--line);
    border-radius:var(--r-md);padding:16px 18px;margin:0 0 14px;
    box-shadow:0 1px 3px rgba(23,25,28,.05)}
  .card h2{font-family:"Sora",sans-serif;font-weight:600;
    font-size:1.05rem;letter-spacing:-.01em;margin:0 0 4px;display:flex;align-items:center;gap:8px}
  .card .hint{color:var(--muted);font-size:.9rem;margin:0 0 14px}
  .card .hint.tail{margin:12px 0 0}
  .card.note{border-left:3px solid var(--accent)}
  .card.note.warn{border-left-color:var(--danger)}
  .card.note h2{font-size:1rem;margin-bottom:6px}

  /* forms */
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  label.fld{display:flex;flex-direction:column;gap:5px;font-size:.85rem;color:var(--muted)}
  input,select{
    font:inherit;color:var(--ink);background:var(--surface);
    padding:10px 12px;border:1.5px solid var(--line);border-radius:var(--r-sm);
  }
  input:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
  input[name=url]{flex:1;min-width:220px}
  input[type=number]{width:96px}
  input[type=file]{padding:9px 11px}

  /* buttons — primary is ink with white text (never sky); one per card */
  button{font:inherit;font-weight:600;cursor:pointer;border:0;border-radius:var(--r-sm);
    padding:11px 17px;transition:background .15s ease,transform .12s ease,border-color .15s ease}
  button:active{transform:translateY(1px)}
  .btn-primary,.btn-accent{background:var(--ink);color:var(--solid-fg)}
  /* Hover to the page's accent, not pure black — #000 went black-on-black in dark
     mode (the label is --solid-fg, which is near-black there). --solid-fg keeps the
     label readable on the accent in both themes (white on blue / dark on sky). */
  .btn-primary:hover,.btn-accent:hover{background:var(--accent);color:var(--solid-fg)}
  .btn-ghost{background:var(--surface);color:var(--ink);border:1px solid var(--line)}
  .btn-ghost:hover{background:var(--bg);border-color:var(--muted)}
  .btn-danger{background:var(--surface);color:var(--danger);border:1px solid #EBC9C9}
  .btn-danger:hover{background:var(--danger-soft);border-color:#DDA8A8}

  /* lists (playlist items, displays, discovered devices) */
  .item{display:flex;flex-wrap:wrap;align-items:center;gap:12px;padding:12px 0;border-top:1px solid var(--line)}
  .item:first-of-type{border-top:0}
  .item .name{flex:1;min-width:0;font-weight:500;display:flex;flex-direction:column;justify-content:center}
  .item .name .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  /* the URL / link, shown IN FULL (wraps to as many rows as it needs) with a copy
     button, so a long Google Slides link can be read and grabbed whole. */
  /* the link gets its OWN full-width row below the item's controls, so a long URL
     wraps across the whole width instead of being squeezed into the name column. */
  .item .srcrow{flex-basis:100%;width:100%;display:flex;align-items:flex-start;gap:8px;margin-top:2px}
  .item .src{font:.72rem/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);
    flex:1;min-width:0;word-break:break-all;overflow-wrap:anywhere}
  .item .copy{flex:none;font:600 .66rem ui-monospace,SFMono-Regular,Menlo,monospace;
    padding:3px 9px;border:1px solid var(--line);border-radius:7px;background:var(--surface);
    color:var(--muted);cursor:pointer;line-height:1.4}
  .item .copy:hover{border-color:var(--accent);color:var(--accent-text)}
  .slidenote{flex-basis:100%;width:100%;font:.72rem ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);margin-top:2px}
  /* A deck being timed right now. A quiet pulsing dot, not a spinner: a row that is
     still working has to look different from one that gave up and wants a click. */
  .slidenote.measuring{color:var(--accent-text);display:flex;align-items:center;gap:7px}
  .slidenote.measuring .dot{width:8px;height:8px;border-radius:50%;background:currentColor;
    flex:0 0 auto;animation:emx-pulse 1.1s ease-in-out infinite}
  @keyframes emx-pulse{0%,100%{opacity:.25}50%{opacity:1}}
  @media(prefers-reduced-motion:reduce){.slidenote.measuring .dot{animation-duration:3s}}
  .secs.auto{font:.72rem ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);align-self:center;padding:8px 10px}
  /* Sound / captions toggles for a video or YouTube item */
  .item .mtogs{flex-basis:100%;width:100%;display:flex;flex-wrap:wrap;gap:16px;margin-top:4px}
  .item .mtog{margin:0}
  .item .mtog label{display:inline-flex;align-items:center;gap:7px;font-size:.82rem;color:var(--muted);cursor:pointer}
  .item .mtog input{accent-color:var(--accent);width:15px;height:15px;cursor:pointer}
  /* per-item 'which screens' control */
  .tgt{margin-top:3px}
  .tgt>summary{font:.7rem ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);cursor:pointer;list-style:none}
  .tgt>summary::-webkit-details-marker{display:none}
  .tgt[open]>summary{color:var(--accent-text)}
  .tgt form{margin:6px 0 2px;display:flex;flex-direction:column;gap:4px}
  .tgt .thint{margin:0;color:var(--muted);font-size:.7rem}
  .tgtbox{display:flex;align-items:center;gap:6px;font-size:.82rem;font-weight:400;cursor:pointer}
  .tgtbox input{width:15px;height:15px;accent-color:var(--accent)}
  .item .meta{font:.78rem ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted)}
  .item form{margin:0}
  .item .secs{display:flex;align-items:center;gap:6px;font-size:.8rem;color:var(--muted)}
  .item .secs input{width:74px;padding:8px 9px}
  .x{background:var(--surface);color:var(--danger);border:1px solid #EBC9C9;
     padding:7px 12px;border-radius:9px;line-height:1}
  .x:hover{background:var(--danger-soft);border-color:#DDA8A8}
  .empty{color:var(--muted)}
  /* reorder arrows + per-item second editing + shuffle toggle */
  .item .ord{display:flex;flex-direction:column;gap:3px;flex:none}
  .mv{background:var(--surface);border:1px solid var(--line);color:var(--muted);
      padding:1px 9px;border-radius:7px;line-height:1.15;font-size:.66rem}
  .mv:hover:not(:disabled){border-color:var(--accent);color:var(--accent-text)}
  .mv:disabled{opacity:.3;cursor:default}
  .item .secs .set{padding:8px 12px}
  .shuffle{display:flex;align-items:center;gap:12px;flex-wrap:wrap;
           margin:0 0 12px;padding:0 0 14px;border-bottom:1px solid var(--line)}
  .shuffle label{display:flex;align-items:center;gap:8px;font-weight:500;cursor:pointer}
  .shuffle input[type=checkbox]{width:18px;height:18px;accent-color:var(--accent)}
  /* health screen rows */
  .hrow{display:flex;gap:12px;padding:9px 0;border-top:1px solid var(--line)}
  .hrow:first-of-type{border-top:0}
  .hk{flex:none;width:150px;color:var(--muted);font-size:.9rem}
  .hv{flex:1;min-width:0;word-break:break-word}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85em}
  .muted{color:var(--muted)}
  .ok-dot{display:inline-block;width:9px;height:9px;border-radius:50%;
          background:var(--ok);margin-right:5px}

  /* pairing code — Sora, sky, the one place a big number carries the accent */
  .code{font-family:"Sora",sans-serif;font-weight:700;font-size:2.2rem;
    letter-spacing:.18em;text-align:center;color:var(--accent);
    background:var(--bg);border:1px solid var(--line);
    border-radius:var(--r-md);padding:18px;margin:6px 0 14px}
  .status{display:flex;align-items:center;gap:10px;color:var(--muted);margin:0 0 12px}
  .status .pulse{position:relative;flex:none;width:12px;height:12px}
  .status .pulse i{position:absolute;inset:0;border-radius:50%;background:var(--accent);
    box-shadow:0 0 0 0 var(--accent-soft);animation:glow 2.8s ease-in-out infinite}
  @keyframes glow{0%,100%{box-shadow:0 0 0 0 var(--accent-soft);opacity:.92}
    50%{box-shadow:0 0 0 10px rgba(47,127,224,0);opacity:1}}

  details summary{cursor:pointer;color:var(--muted);font-size:.9rem;list-style:none}
  details summary::-webkit-details-marker{display:none}
  details[open] summary{margin-bottom:10px}

  /* tooltips: CSS-only, driven by data-tip on a small (i) affordance */
  .tip{position:relative;display:inline-flex;align-items:center;justify-content:center;
    width:17px;height:17px;border-radius:50%;border:1px solid var(--line);
    font-size:.62rem;font-weight:700;color:var(--muted);
    background:var(--surface);cursor:help;vertical-align:middle;user-select:none}
  .tip:hover{border-color:var(--accent);color:var(--accent-text)}
  .tip::after{
    content:attr(data-tip);position:absolute;left:50%;bottom:calc(100% + 9px);
    transform:translateX(-50%);width:max-content;max-width:250px;
    background:var(--ink);color:var(--solid-fg);font-family:"Hanken Grotesk",sans-serif;
    font-size:.78rem;font-weight:400;line-height:1.4;letter-spacing:normal;
    text-transform:none;text-align:left;padding:9px 11px;border-radius:10px;
    opacity:0;visibility:hidden;transition:opacity .14s ease;
    pointer-events:none;z-index:20;box-shadow:0 6px 22px rgba(23,25,28,.18)}
  .tip::before{
    content:"";position:absolute;left:50%;bottom:calc(100% + 3px);
    transform:translateX(-50%);border:6px solid transparent;border-top-color:var(--ink);
    opacity:0;visibility:hidden;transition:opacity .14s ease;z-index:20}
  .tip:hover::after,.tip:hover::before{opacity:1;visibility:visible}

  /* settings drawer */
  .scrim{position:fixed;inset:0;background:rgba(23,25,28,.28);
    opacity:0;visibility:hidden;transition:opacity .2s ease;z-index:40}
  .drawer{
    position:fixed;top:0;right:0;height:100%;width:min(420px,92vw);
    background:var(--bg);border-left:1px solid var(--line);
    box-shadow:-18px 0 50px rgba(23,25,28,.16);
    transform:translateX(100%);transition:transform .24s ease;
    z-index:50;overflow-y:auto;padding:24px}
  body.settings-open .scrim{opacity:1;visibility:visible}
  body.settings-open .drawer{transform:translateX(0)}
  .drawer-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}
  .drawer-head h2{font-family:"Sora",sans-serif;font-weight:600;font-size:1.3rem;margin:0}
  .drawer .close{border:1px solid var(--line);background:var(--surface);
    border-radius:10px;padding:7px 12px;font:inherit;cursor:pointer;color:var(--muted)}
  .drawer .close:hover{background:var(--bg);border-color:var(--muted)}
  .drawer .section{background:var(--surface);border:1px solid var(--line);
    border-radius:var(--r-md);padding:16px 18px;margin-bottom:14px}
  .drawer .section h3{font-family:"Sora",sans-serif;font-weight:600;
    font-size:1rem;margin:0 0 4px;display:flex;align-items:center;gap:8px}
  .drawer .section p{color:var(--muted);font-size:.88rem;margin:0 0 12px}
  .drawer .section .now{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--accent-text)}
  .drawer .full{width:100%;justify-content:center;display:flex}
  .drawer .section input{width:100%;margin-bottom:10px}
  .drawer .check{display:flex;align-items:center;gap:9px;cursor:pointer;font-weight:600;color:var(--ink)}
  .drawer .check input{width:18px;height:18px;flex:none;margin:0;accent-color:var(--accent)}

  /* light/dark/auto segmented control */
  .themeopts{display:flex;gap:6px}
  .themebtn{flex:1;padding:9px;border:1px solid var(--line);background:var(--surface);
    color:var(--ink);border-radius:var(--r-sm);font:inherit;font-size:.85rem;cursor:pointer}
  .themebtn:hover{border-color:var(--muted)}
  .themebtn.on{background:var(--accent-soft);border-color:var(--accent);color:var(--accent-text)}

  :focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}

  @media (max-width:560px){
    body{padding:18px}
    .top{gap:9px}
    .item{flex-wrap:wrap}
  }
  @media (prefers-reduced-motion:reduce){
    .status .pulse i{animation:none}
  }
"""

# Friendly, non-technical tooltip copy reused across pages.
_TIPS = {
    "display": "A display is a screen that just shows content — slides, images, "
               "or web pages that a controller sends to it.",
    "controller": "A controller is the device you manage everything from. It holds "
                  "the content and pushes it out to your displays.",
    "pairing": "Pairing links a display to this controller. The display shows a short "
               "code; type that code here (or on the display) to connect them — like "
               "pairing a Bluetooth speaker.",
    "push": "Push sends this controller's current playlist to every paired display, "
            "so they all start showing the latest content.",
    "slides": "In Google Slides choose File → Share → Publish to web, then "
              "paste the link it gives you. That makes a view-only link your displays "
              "can show without anyone signing in.",
    "playlist": "The playlist is the list of things that rotate on screen, in order. "
                "Set how many seconds each item stays up.",
    "password": "Optional lock. Set a username and password and the panel asks for "
                "them before anyone can change content or settings. Your screens "
                "keep playing without it.",
}


def _tip(key: str) -> str:
    """Small (i) affordance with a CSS-only hover tooltip."""
    return f'<span class="tip" data-tip="{_esc(_TIPS[key])}" aria-label="More info">i</span>'


def _settings_drawer(cfg: dict, role: str) -> str:
    """Slide-out Settings drawer: rename, role switch, password lock, screen link."""
    other = "display" if role == "controller" else "controller"
    other_label = "Display" if other == "display" else "Controller"
    role_label = "Controller" if role == "controller" else "Display"
    other_tip = _TIPS["display"] if other == "display" else _TIPS["controller"]

    if auth.has_credentials():
        uname = _esc(auth.admin_username() or "admin")
        pw_section = f"""
        <div class="section">
          <h3>Password {_tip('password')}</h3>
          <p>This panel is <span class="now">locked</span>. Only someone with the
            password can change content or settings.</p>
          <details>
            <summary>Change password</summary>
            <form method="post" action="/api/password/set" style="margin-top:10px">
              <input name="username" value="{uname}" placeholder="username" required>
              <input name="password" type="password" placeholder="new password (min 6)" required>
              <p class="hint">Everyone else who is signed in will need the new password.</p>
              <button class="btn-primary full" type="submit">Save new password</button>
            </form>
          </details>
          <form method="get" action="/password/clear" style="margin-top:10px">
            <button class="btn-danger full" type="submit">Remove the password&hellip;</button>
          </form>
          <form method="post" action="/logout" style="margin-top:10px">
            <button class="btn-ghost full" type="submit">Log out</button>
          </form>
        </div>"""
    else:
        pw_section = f"""
        <div class="section">
          <h3>Password {_tip('password')}</h3>
          <p>Optional, but recommended. Lock this panel so only people with the
            password can change content or settings — your screens keep playing
            either way.</p>
          <form method="post" action="/api/password/set">
            <input name="username" value="admin" placeholder="username" required>
            <input name="password" type="password" placeholder="password (min 6 characters)" required>
            <button class="btn-accent full" type="submit">Lock this panel</button>
          </form>
        </div>"""

    # A controller can double as a display — play the playlist on its own screen too.
    if role == "controller":
        dh_on = " checked" if cfg.get("display_content", True) else ""
        display_here_section = f"""
        <div class="section">
          <h3>This screen</h3>
          <p>Play the playlist on this controller&rsquo;s own screen too, not only push
            it to your displays. Turn off to make this a management-only box.</p>
          <form method="post" action="/api/display-here">
            <label class="check"><input type="checkbox" name="show" value="on"{dh_on}
              onchange="this.form.submit()"> Show content on this screen</label>
          </form>
        </div>"""
    else:
        display_here_section = ""

    # Pairing controls live here, in Settings, not on the display's home page: a
    # screen-level Unpair button was being pressed to "fix" a screen that looked
    # stuck, cutting it off its controller. Unpair still goes through the
    # confirmation screen at /pair/forget.
    pairing_section = ""
    if role != "controller" and pairing.is_claimed():
        _ctl = pairing.get_controller() or {}
        _cn = _esc(_ctl.get("name") or "its controller")
        _ca = f' (<span class="now">{_esc(_ctl["address"])}</span>)' if _ctl.get("address") else ""
        pairing_section = f"""
        <div class="section">
          <h3>Pairing {_tip('pairing')}</h3>
          <p>Paired to <span class="now">{_cn}</span>{_ca}, which sends this screen its content.</p>
          <form method="get" action="/pair/start">
            <button class="btn-ghost full" type="submit">Re-pair this screen&hellip;</button>
          </form>
          <form method="get" action="/pair/forget">
            <button class="btn-danger full" type="submit">Unpair this screen&hellip;</button>
          </form>
        </div>"""

    # Sound output. The switch is done by a privileged helper, so what this can offer
    # depends on whether that helper is installed and what it last reported.
    a_state = audio.state()

    def _matches(pin: str, out: dict) -> bool:
        # Mirror the helper's own order: exact id, exact name, then a case-insensitive
        # part of a name. Keeping these in step is what makes the ticked option the
        # one the device will actually use.
        return bool(pin) and (pin == out["id"] or pin == out["name"]
                              or pin.lower() in out["name"].lower())

    if not audio.installed():
        # In-app "Update now" only replaces app code, so a device updated that way has
        # the picker but not the helper behind it. Say so instead of offering a
        # control that would silently do nothing.
        audio_section = """
        <div class="section">
          <h3>Sound output</h3>
          <p>Sound still plays through the TV over HDMI. To choose a different output
            from here, this device needs a full update &mdash; run
            <span class="now">sudo eximaro-update-full</span> on it once.</p>
        </div>"""
    elif a_state["outputs"]:
        pinned, current = a_state["pinned"], a_state["current"]
        matched = [o for o in a_state["outputs"] if _matches(pinned, o)]
        chosen = matched[0] if matched else None   # the helper takes the first match too
        opts = ['<option value="auto"%s>Automatic (the TV over HDMI)</option>'
                % ("" if pinned else " selected")]
        for out in a_state["outputs"]:
            label = out["name"] + (" — in use now" if out["id"] == current else "")
            opts.append('<option value="%s"%s>%s</option>'
                        % (_esc(out["name"]),
                           " selected" if out is chosen else "",
                           _esc(label)))
        note = ""
        if pinned and chosen is None:
            note = ('<p class="hint">Chosen output <b>%s</b> isn&rsquo;t available right now, '
                    'so sound is playing through whatever the device found.</p>' % _esc(pinned))
        elif audio.pending():
            note = '<p class="hint">Applying&hellip; reopen Settings in a moment to confirm.</p>'
        audio_section = f"""
        <div class="section">
          <h3>Sound output</h3>
          <p>Where videos with &ldquo;Play sound&rdquo; are heard. Automatic sends sound
            to the TV over HDMI, which is right for nearly every setup &mdash; change it
            for a soundbar, a headphone jack, or a second HDMI port.</p>
          {note}
          <form method="post" action="/api/audio">
            <select name="output" onchange="this.form.submit()">{''.join(opts)}</select>
            <noscript><button class="btn-primary full" type="submit">Save</button></noscript>
          </form>
          <form method="post" action="/api/audio/refresh">
            <button class="btn-ghost full" type="submit">Re-scan for outputs</button>
          </form>
        </div>"""
    else:
        audio_section = """
        <div class="section">
          <h3>Sound output</h3>
          <p>No sound outputs found yet. Connect the screen (or a speaker) and re-scan.</p>
          <form method="post" action="/api/audio/refresh">
            <button class="btn-ghost full" type="submit">Re-scan for outputs</button>
          </form>
        </div>"""

    upd = updater.status()
    upd_line = (f'<p>Last update: <span class="now">{_esc(upd["state"])}</span> &mdash; '
                f'{_esc(upd.get("detail", ""))}</p>') if upd.get("state") else ""
    software_section = f"""
        <div class="section">
          <h3>Software</h3>
          <p>Version <span class="now">{_esc(updater.current_version())}</span>. Updates are
            staged and health-checked &mdash; if a new version doesn't start, the device
            rolls back to the previous one automatically.</p>
          {upd_line}
          <form method="get" action="/update">
            <button class="btn-accent full" type="submit">Update now&hellip;</button>
          </form>
        </div>"""

    # WiFi: show what this device is on now, with a link to change/add a network.
    real = [a for a in discovery.labeled_ips() if not a["ip"].startswith(discovery.AP_SUBNET)]
    if any(a["label"] == "Wi-Fi" for a in real):
        ssid = wifi.status().get("ssid")
        net_line = "This device is on Wi-Fi" + (
            f' &mdash; <span class="now">{_esc(ssid)}</span>.' if ssid else ' <span class="now">Wi-Fi</span>.')
    elif any(a["label"] == "Ethernet" for a in real):
        net_line = 'This device is on <span class="now">Ethernet</span>.'
    elif real:
        net_line = f'This device is on <span class="now">{_esc(real[0]["label"])}</span>.'
    else:
        net_line = 'This device is <span class="now">not on a network</span>.'
    wifi_section = f"""
        <div class="section">
          <h3>WiFi</h3>
          <p>{net_line} Connect it to a WiFi network, or switch which one it uses.</p>
          <a class="btn-ghost full" href="/wifi-setup" style="text-decoration:none">Set up / change WiFi</a>
        </div>"""

    return f"""
      <div class="scrim" onclick="toggleSettings()"></div>
      <aside class="drawer" aria-label="Settings">
        <div class="drawer-head">
          <h2>Settings</h2>
          <button class="close" onclick="toggleSettings()">Close</button>
        </div>

        <div class="section">
          <h3>Device name</h3>
          <p>Shown to controllers on the network. This also renames the device itself.</p>
          <form method="post" action="/api/name">
            <input name="name" value="{_esc(cfg['name'])}" placeholder="device name" required>
            <button class="btn-primary full" type="submit">Save name</button>
          </form>
        </div>

        <div class="section">
          <h3>Appearance</h3>
          <p>Light, dark, or match your device.</p>
          <div class="themeopts">
            <button type="button" class="themebtn" data-theme-opt="light" onclick="setTheme('light')">Light</button>
            <button type="button" class="themebtn" data-theme-opt="dark" onclick="setTheme('dark')">Dark</button>
            <button type="button" class="themebtn" data-theme-opt="auto" onclick="setTheme('auto')">Auto</button>
          </div>
        </div>

        {wifi_section}

        {audio_section}

        <div class="section">
          <h3>Role <span class="tip" data-tip="{_esc(other_tip)}" aria-label="More info">i</span></h3>
          <p>This device is currently a <span class="now">{role_label}</span>.</p>
          <form method="get" action="/role">
            <input type="hidden" name="role" value="{other}">
            <button class="btn-accent full" type="submit">Switch to {other_label}&hellip;</button>
          </form>
        </div>

        {display_here_section}

        {pairing_section}

        {pw_section}

        <div class="section">
          <h3>Health &amp; activity</h3>
          <p>See device status and a log of recent changes.</p>
          <a class="btn-ghost full" href="/health" style="text-decoration:none">Open health screen</a>
        </div>

        {software_section}

        <div class="section">
          <h3>Full-screen view</h3>
          <p>Open the page a screen shows when running as a display.</p>
          <a class="btn-ghost full" href="/screen?preview=1" style="text-decoration:none">Open full-screen view</a>
        </div>
      </aside>
    """


_SETTINGS_JS = """
  <script>
    function toggleSettings(){document.body.classList.toggle('settings-open');}
    document.addEventListener('keydown',function(e){
      if(e.key==='Escape')document.body.classList.remove('settings-open');
    });
    // Copy a link to the clipboard. The panel is served over plain HTTP, where the
    // modern clipboard API is blocked, so fall back to a hidden-textarea + execCommand.
    function copyUrl(b){
      var url=b.getAttribute('data-copy')||'';
      var ok=function(){var t=b.textContent;b.textContent='Copied';setTimeout(function(){b.textContent=t;},1200);};
      if(navigator.clipboard&&navigator.clipboard.writeText){
        navigator.clipboard.writeText(url).then(ok,function(){copyFallback(url,ok);});
      }else{copyFallback(url,ok);}
    }
    function copyFallback(text,ok){
      var ta=document.createElement('textarea');ta.value=text;
      ta.style.position='fixed';ta.style.top='0';ta.style.opacity='0';
      document.body.appendChild(ta);ta.focus();ta.select();
      try{document.execCommand('copy');ok();}catch(e){}
      document.body.removeChild(ta);
    }
    // Light / Dark / Auto. 'auto' clears the choice so the device's own setting wins.
    function setTheme(t){
      try{
        if(t==='auto'){localStorage.removeItem('eximaro-theme');document.documentElement.removeAttribute('data-theme');}
        else{localStorage.setItem('eximaro-theme',t);document.documentElement.setAttribute('data-theme',t);}
      }catch(e){}
      markTheme(t);
    }
    function markTheme(t){
      var cur=t;
      if(!cur){try{cur=localStorage.getItem('eximaro-theme')||'auto';}catch(e){cur='auto';}}
      var opts=document.querySelectorAll('[data-theme-opt]');
      for(var i=0;i<opts.length;i++){opts[i].classList.toggle('on',opts[i].getAttribute('data-theme-opt')===cur);}
    }
    markTheme();
  </script>
"""


def _header(cfg: dict, role: str) -> str:
    """Consistent header: the eximaro wordmark, device name, and role badge."""
    badge_label = "Controller" if role == "controller" else "Display"
    return f"""
      <header class="top">
        <span class="brand">e<span class="x">x</span>imaro</span>
        <span class="spacer"></span>
        <span class="name-chip">{_esc(cfg['name'])}</span>
        <span class="badge {role}">{badge_label}</span>
      </header>
    """


def _nav(active: str, role: str) -> str:
    """The pill nav. A controller manages screens and a separate content library,
    so it gets both tabs; a display's content lives on its own home, so it doesn't.
    Settings opens the slide-out drawer. Every tab is a real destination."""
    def on(key):
        return ' class="on"' if key == active else ""
    settings = f'<a href="#" onclick="toggleSettings();return false;"{on("settings")}>Settings</a>'
    if role == "controller":
        tabs = (f'<a href="/"{on("home")}>Screens</a>'
                f'<a href="/content"{on("content")}>Content</a>'
                f'<a href="/health"{on("health")}>Health</a>'
                f'{settings}')
    else:
        tabs = (f'<a href="/"{on("home")}>Home</a>'
                f'<a href="/health"{on("health")}>Health</a>'
                f'{settings}')
    return f'\n      <nav class="nav" aria-label="Sections">{tabs}</nav>\n    '


# A full-screen "working…" overlay + helpers, dropped on every panel page. It gives
# the operator immediate feedback on actions that otherwise look like nothing is
# happening (adding a link, uploading, updating). Plain string (not an f-string) so
# its CSS/JS braces stay literal.
_WORKING_HTML = """
  <div id="working" class="working" role="status" aria-live="polite" aria-hidden="true">
    <div class="working-box"><div class="spinner"></div><div id="workingMsg">Working…</div></div>
  </div>
  <style>
    .working{position:fixed;inset:0;z-index:200;display:none;align-items:center;justify-content:center;
      background:rgba(10,12,14,.55)}
    .working.on{display:flex}
    .working-box{background:var(--surface,#fff);color:var(--ink,#17191C);border-radius:14px;
      padding:24px 30px;display:flex;flex-direction:column;align-items:center;gap:14px;text-align:center;
      font-weight:600;max-width:80vw;box-shadow:0 12px 44px rgba(0,0,0,.4)}
    .spinner{width:34px;height:34px;border-radius:50%;border:3px solid var(--line,#E6E8EA);
      border-top-color:var(--accent,#2F7FE0);animation:emxspin .8s linear infinite}
    @keyframes emxspin{to{transform:rotate(360deg)}}
    @media(prefers-reduced-motion:reduce){.spinner{animation-duration:2.4s}}
  </style>
  <script>
    function showWorking(m){var w=document.getElementById('working');if(!w)return;
      document.getElementById('workingMsg').textContent=m||'Working…';
      w.classList.add('on');w.setAttribute('aria-hidden','false');}
    function hideWorking(){var w=document.getElementById('working');if(!w)return;
      w.classList.remove('on');w.setAttribute('aria-hidden','true');}
    // Coming back via the back button (bfcache) must not leave a stale overlay up.
    // Back to a page from the browser's cache shows it as it was left, so a box unticked on
    // the way to a confirmation screen would still look unticked. Put every box back to
    // what the page said when it was served.
    window.addEventListener('pageshow',function(){hideWorking();
      document.querySelectorAll('input[type=checkbox]').forEach(function(c){c.checked=c.defaultChecked;});});
    // Software update: poll the ACTUAL update status (not /healthz) so we react to
    // the real outcome. A failed update never restarts the app, so there is no
    // restart to detect — only the status says 'failed'. Must be called as
    // onsubmit="startUpdate();return false;" — an async function returns a Promise,
    // which is truthy, so `return startUpdate()` would NOT cancel the native submit.
    async function startUpdate(){
      // The question was asked on the /update screen; this only runs from its yes.
      showWorking('Updating\\u2026 This page comes back by itself when the update is done.');
      // Remember the previous status timestamp, so a stale result from a past update
      // (e.g. a leftover 'done') can't make us reload before this update even starts.
      var before='';
      try{before=((await (await fetch('/api/update-status',{cache:'no-store'})).json())||{}).when||'';}catch(e){}
      try{await fetch('/api/update',{method:'POST',body:new URLSearchParams({confirm:'yes'})});}catch(e){}
      var tries=0;
      var iv=setInterval(async function(){
        tries++;
        try{
          var s=(await (await fetch('/api/update-status',{cache:'no-store'})).json())||{};
          if(s.when&&s.when!==before){                 // a fresh status from THIS update
            // Home, not a reload: a reload would land back on the /update question.
            // A failed update says so in the "Update problem" banner on every page.
            if(s.state==='done'){clearInterval(iv);location.assign('/');return;}
            if(s.state==='failed'||s.state==='rolled_back'){
              clearInterval(iv);hideWorking();location.assign('/');return;
            }
          }
        }catch(e){}                              // the server may be restarting mid-swap
        if(tries>150){clearInterval(iv);location.assign('/');}   // ~5 min hard cap
      },2000);
      return false;
    }
  </script>"""


def _update_alert_banner() -> str:
    """A persistent, dismissable banner when the last update failed or rolled back.
    On every panel page — a failed update on an unattended device must never be
    silent. It survives reboots (the status lives on disk) until a later update
    succeeds or the operator dismisses it."""
    msg = updater.alert()
    if not msg:
        return ""
    return f"""
      <div class="card note warn" style="display:flex;gap:12px;align-items:flex-start;justify-content:space-between">
        <div>
          <h2 style="margin-top:0">Update problem</h2>
          <p class="hint">{_esc(msg)}</p>
        </div>
        <form method="post" action="/api/update/ack" style="margin:0">
          <button class="btn-ghost" type="submit">Dismiss</button>
        </form>
      </div>"""


def _confirm_card(title: str, lead: str, bullets, keep_label: str, keep_href: str,
                  yes_label: str, action: str, fields: dict = None, closing: str = "",
                  danger: bool = True, onsubmit: str = "") -> str:
    """Every "are you sure?" screen in the panel, built the same way.

    The people pressing these buttons are not technical, so before anything happens
    each screen says in plain words what will happen: to the TV, to the other screens,
    and how to undo it. The safe choice is the main button. The yes posts confirm=yes,
    and the route behind it refuses to act without that, so a stray tap, an old cached
    page or a bookmarked form lands back on the explanation instead.

    `title`, `lead`, `bullets` and `closing` are HTML: escape every real name in them."""
    points = "".join(f"<li>{b}</li>" for b in bullets if b)
    points_html = f'<ul class="hint">{points}</ul>' if points else ""
    closing_html = f'<p class="hint">{closing}</p>' if closing else ""
    hidden = "".join(f'<input type="hidden" name="{_esc(k)}" value="{_esc(v)}">'
                     for k, v in (fields or {}).items())
    submit = f' onsubmit="{onsubmit}"' if onsubmit else ""
    yes_class = "btn-danger" if danger else "btn-ghost"
    return f"""
          <div class="card" style="overflow-wrap:anywhere">
            <h2 style="display:block">{title}</h2>
            <p>{lead}</p>
            {points_html}
            {closing_html}
            <div class="row">
              <form method="get" action="{keep_href}">
                <button class="btn-primary" type="submit">{keep_label}</button>
              </form>
              <form method="post" action="{action}"{submit}>
                <input type="hidden" name="confirm" value="yes">{hidden}
                <button class="{yes_class}" type="submit">{yes_label}</button>
              </form>
            </div>
          </div>"""


def _nothing_card(title: str, hint: str, back_href: str = "/", back_label: str = "Back") -> str:
    """Shown in place of a confirmation when there is nothing left to confirm: the item
    is already gone, the screen isn't paired, the update is already on its way."""
    return f"""
          <div class="card">
            <h2>{title}</h2>
            <p class="hint">{hint}</p>
            <form method="get" action="{back_href}"><button class="btn-ghost" type="submit">{back_label}</button></form>
          </div>"""


def _names(names) -> str:
    """'A', 'A and B' or 'A, B and C': for naming the screens something affects."""
    names = [n for n in names if n]
    if len(names) < 2:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _count(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _display_names(displays) -> list:
    return [_esc(d.get("name") or d["device_id"][:8]) for d in displays]


def _go_to_controller(controller: dict) -> str:
    """A link to the controller's Content page, when its address is known."""
    addr = pairing.lan_address(controller.get("address"))
    if not addr:
        return ""
    name = _esc(controller.get("name") or "its controller")
    return (f' <a href="http://{_esc(addr)}:{config.PORT}/content">Open {name}&rsquo;s '
            f'Content page</a>.')


_WAITING = "&ldquo;Waiting for content&hellip;&rdquo;"


def _remove_item_body(cfg: dict, item_id: str, device_id: str) -> str:
    """Before an item comes off the playlist. It names the exact item - a tap on the
    wrong row of a phone-sized list is the usual mistake - and which screens lose it."""
    role = cfg.get("role") or "display"
    back = "/content" if role == "controller" else "/"
    items = library.list_items()
    item = next((i for i in items if i["id"] == item_id), None)
    if not item:
        return _nothing_card("That item isn&rsquo;t in the playlist any more",
                             "Someone may have removed it already. Nothing has changed.", back,
                             "Back to the playlist")
    name = _esc(item.get("name") or "this item")
    device = _esc(cfg["name"])
    rest = [i for i in items if i["id"] != item_id]
    bullets = []
    if role == "controller":
        playing, emptied = [], []
        screens = [(device_id, f"{device}&rsquo;s own screen")] if cfg.get("display_content", True) else []
        screens += [(d["device_id"], n) for d, n in zip(pairing.list_displays(),
                                                        _display_names(pairing.list_displays()))]
        for sid, label in screens:
            if item in sync.items_for_display(items, sid):
                playing.append(label)
                if not sync.items_for_display(rest, sid):
                    emptied.append(label)
        bullets.append(f"It comes off {_names(playing)}." if playing
                       else "No screen is showing it right now.")
        if emptied:
            bullets.append(f"{_names(emptied)} will have nothing left to play and will show {_WAITING}")
    elif sync.screen_items() is not None:
        ctl = _esc((pairing.get_controller() or {}).get("name") or "its controller")
        bullets.append(f"This screen is playing what {ctl} sends, so nothing changes on it right now.")
    elif item in sync.items_for_display(items, device_id):
        bullets.append("It comes off this screen.")
        if not sync.items_for_display(rest, device_id):
            bullets.append(f"This screen will have nothing left to play and will show {_WAITING}")
    else:
        bullets.append("This screen isn&rsquo;t showing it right now.")
    kind, ref = item.get("type"), str(item.get("ref") or "")
    if kind == "slideshow":
        bullets.append(f"Its slides are deleted from {device}. To show it again, you will need "
                       f"the original PowerPoint file.")
    elif kind == "image" or (kind == "video" and not ref.startswith("http")):
        bullets.append(f"Its file is deleted from {device}. To show it again, you will need the "
                       f"original file.")
    else:
        link = f"https://www.youtube.com/watch?v={item['yt_id']}" if item.get("yt_id") else ref
        # Most links are named by the link itself, which the title already shows.
        if (item.get("name") or "") in (link, ref):
            bullets.append("It is only a link, so you can add it again later.")
        else:
            bullets.append(f'It is only a link, so you can add it again later with this link: '
                           f'<span class="mono">{_esc(link)}</span>')
    closing = ""
    if role == "controller" and pairing.list_displays():
        closing = ("Only want it off some screens? Use &ldquo;On:&rdquo; under its name to pick "
                   "its screens. Nothing is deleted that way.")
    return _confirm_card(f"Remove &ldquo;{name}&rdquo;?", "If you remove it from the playlist:",
                         bullets, "Keep it", back, "Yes, remove it", "/api/content/remove",
                         fields={"item_id": item_id}, closing=closing)


def _remove_display_body(cfg: dict, target: str, device_id: str) -> str:
    """Before a controller stops sending to one of its displays."""
    device = _esc(cfg["name"])
    displays = pairing.list_displays()
    d = next((x for x in displays if x["device_id"] == target), None)
    if not d:
        return _nothing_card(f"That display isn&rsquo;t paired with {device}",
                             "It may have been unpaired already. Nothing has changed.")
    dname = _esc(d.get("name") or d["device_id"][:8])
    # Items aimed at this display and at no other screen this controller still runs
    # would play nowhere once it is gone - say so, by name.
    live = {x["device_id"] for x in displays} | {device_id}
    only_it = [f"&ldquo;{_esc(i.get('name') or 'an item')}&rdquo;" for i in library.list_items()
               if i.get("targets") and set(i["targets"]) & live == {d["device_id"]}]
    bullets = [f"{dname} keeps showing what it has now, but nothing you change here will reach it.",
               f"{dname}&rsquo;s own panel will still say it is controlled by {device}.",
               "Nothing changes on your other screens."]
    if only_it:
        one = len(only_it) == 1
        bullets.append(f"{_names(only_it)} only show{'s' if one else ''} on {dname}, so after this "
                       f"{'it' if one else 'they'} will show on no screen.")
    addr = pairing.lan_address(d.get("address"))
    try:
        port = int(d.get("port") or config.PORT)
    except (TypeError, ValueError):
        port = config.PORT
    panel = (f'<a href="http://{addr}:{port}/">{dname}&rsquo;s own panel</a>' if addr
             else f"{dname}&rsquo;s own panel")
    bullets.append(f"To pair it again later, open {panel}, go to Settings, choose "
                   f"&ldquo;Re-pair this screen&hellip;&rdquo; and press &ldquo;Yes, show a pairing "
                   f"code&rdquo;. Then enter that code under &ldquo;Find displays&rdquo; here.")
    return _confirm_card(f"Unpair {dname}?", f"{dname} gets its content from {device}. If you unpair it:",
                         bullets, "Keep it paired", "/", f"Yes, unpair {dname}", "/api/displays/remove",
                         fields={"device_id": d["device_id"]},
                         closing=f"Only want to change what {dname} shows? Change the playlist on the "
                                 f"Content page instead.")


def _clear_pushed_body(cfg: dict, device_id: str) -> str:
    """Before a display drops the content its controller pushed to it."""
    pushed = sync.screen_items()
    if pushed is None or cfg.get("role") == "controller":
        return _nothing_card("Nothing from a controller is on this screen",
                             "A controller always plays its own playlist." if cfg.get("role") == "controller"
                             else "There is nothing to remove.")
    controller = pairing.get_controller() or {}
    ctl = _esc(controller.get("name") or "its controller")
    own = sync.items_for_display(library.list_items(), device_id)
    if pushed:
        one = len(pushed) == 1
        lead = (f"This screen is playing {_count(len(pushed), 'item')} that {ctl} sent. "
                f"If you remove {'it' if one else 'them'}:")
        gone = f"{'It comes' if one else 'They come'} off this screen straight away"
        first = (f"{gone}, and it plays its own {_count(len(own), 'item')} instead." if own else
                 f"{gone}. It has nothing of its own to play, so it will show {_WAITING}")
    else:
        # A controller sends an empty list to a screen none of its items are aimed at.
        lead = f"{ctl} sent this screen an empty playlist, so it shows {_WAITING}. If you remove it:"
        first = (f"It plays its own {_count(len(own), 'item')} instead." if own
                 else "Nothing changes on the TV.")
    bullets = [
        first,
        (f"This screen stays paired. The next time {ctl} sends (when anything changes there, or "
         f"when someone presses &ldquo;Push to all displays&rdquo;), this screen plays {ctl}&rsquo;s "
         f"playlist again." if pairing.is_claimed() else ""),
        f"Nothing changes on {ctl} or on your other screens.",
    ]
    return _confirm_card(f"Remove {ctl}&rsquo;s content from this screen?", lead, bullets,
                         "Keep it on this screen", "/", "Yes, remove it from this screen",
                         "/api/received/clear",
                         closing=(f"Only want to change what this screen shows? Change it on {ctl} "
                                  f"instead. This screen updates by itself.{_go_to_controller(controller)}"))


def _role_switch_body(cfg: dict, to_role: str, device_id: str) -> str:
    """Before a device changes role. Both directions change what screens show, so both
    say exactly which screens and how, and how to switch back."""
    device = _esc(cfg["name"])
    bullets = []
    if to_role == "controller":
        controller = pairing.get_controller() or {}
        ctl = _esc(controller.get("name") or "its controller")
        claimed = pairing.is_claimed()
        pushed_now = sync.screen_items() is not None
        own = len(sync.items_for_display(library.list_items(), device_id))
        shows_own = cfg.get("display_content", True)
        if pushed_now:
            lead = f"{device} is a display that shows what {ctl} sends. If you switch it to Controller:"
            bullets.append(f"Its screen stops showing what {ctl} sends. {ctl} still lists it, but "
                           f"changes made there won&rsquo;t show on it.")
        elif claimed:
            lead = f"{device} is a display paired to {ctl}. If you switch it to Controller:"
            bullets.append(f"{ctl} can keep sending to it, but nothing it sends will show while it "
                           f"is a Controller.")
        else:
            lead = f"{device} is a display. If you switch it to Controller:"
        if own and not shows_own:
            bullets.append(f"&ldquo;Show content on this screen&rdquo; is turned off, so its screen will "
                           f"show {_WAITING}. To play its own {_count(own, 'item')}, tick that box in Settings.")
        elif own:
            bullets.append(f"Its screen plays {device}&rsquo;s own {_count(own, 'item')} instead."
                           if pushed_now else "Its screen keeps playing what it plays now.")
        elif pushed_now:
            bullets.append(f"It has nothing of its own to play, so its screen will show {_WAITING}")
        bullets.append("This panel changes: its playlist moves to a new Content page, and it gets a "
                       "Screens page for pairing displays.")
        if not promote.has_conversion_tools():
            bullets.append("It also downloads PowerPoint support from the internet, which takes a "
                           "while. Its screen keeps playing meanwhile.")
        bullets.append("To undo, open Settings and choose &ldquo;Switch to Display&hellip;&rdquo;."
                       + (f" It is still paired to {ctl}, so what {ctl} sends shows again."
                          if claimed else ""))
        closing = (f"Only want to change what this screen shows? Change it on {ctl} instead. "
                   f"This screen updates by itself.{_go_to_controller(controller)}" if claimed else "")
        return _confirm_card(f"Switch {device} to Controller?", lead, bullets, "Keep it a Display", "/",
                             "Yes, switch to Controller", "/api/role", fields={"role": "controller"},
                             closing=closing)
    displays = pairing.list_displays()
    names = _names(_display_names(displays))
    one = len(displays) == 1
    lead = (f"{device} is the controller for {names}. If you switch it to Display:" if displays
            else f"{device} is a controller with no displays paired. If you switch it to Display:")
    if displays:
        bullets.append(f"{names} {'stops' if one else 'stop'} getting changes from {device}. "
                       f"{'It keeps' if one else 'They keep'} showing what "
                       f"{'it has' if one else 'they have'} now.")
    if sync.screen_items() is not None:
        # A device that was a display before still holds what its controller sent it, and
        # a display plays that ahead of its own playlist.
        old = _esc((pairing.get_controller() or {}).get("name") or "a controller")
        bullets.append(f"{device}&rsquo;s own screen plays what {old} sent it, not its own playlist. "
                       f"To play its playlist, use &ldquo;Remove pushed content&hellip;&rdquo; on its "
                       f"Home afterwards.")
    elif not cfg.get("display_content", True):
        bullets.append(f"{device}&rsquo;s own screen starts playing its playlist.")
    else:
        bullets.append(f"{device}&rsquo;s own screen keeps playing its playlist.")
    bullets.append("This panel changes: the Content page goes away, and the playlist moves to Home.")
    bullets.append("To undo, open Settings and choose &ldquo;Switch to Controller&hellip;&rdquo;."
                   + (f" {names} then get{'s' if one else ''} changes from it again." if displays else ""))
    return _confirm_card(f"Switch {device} to Display?", lead, bullets, "Keep it a Controller", "/",
                         "Yes, switch to Display", "/api/role", fields={"role": "display"})


def _update_running() -> bool:
    """True while an update is under way: asked for and not picked up yet, or picked up
    and still running. The helper deletes the request the moment it starts, so the
    request alone goes quiet during the download and build - exactly when a second
    press would queue a second update. A "running" status more than an hour old is
    left from an interrupted update (a power cut, say) and does not count."""
    if updater.in_progress():
        return True
    st = updater.status()
    if st.get("state") != "running":
        return False
    try:
        when = datetime.fromisoformat(str(st.get("when", "")))
    except ValueError:
        return False
    if when.tzinfo is None:
        return False
    return (datetime.now(timezone.utc) - when).total_seconds() < 3600


def _update_body(cfg: dict) -> str:
    """Before a software update: what the screen does while it happens, and that a
    failed update puts itself back."""
    device = _esc(cfg["name"])
    if _update_running():
        return _nothing_card("An update is already on its way",
                             f"{device} is already getting the newest version, so there is no need to "
                             f"press it again. When it restarts, its screen goes dark for a few "
                             f"seconds, then carries on.")
    role = cfg.get("role")
    others = bool(pairing.list_displays()) if role == "controller" else pairing.is_claimed()
    bullets = [
        f"{device} downloads the newest version of its app from the internet. Its screen carries on "
        f"as normal meanwhile.",
        "Then it restarts. Its screen goes dark for a few seconds, then carries on.",
        f"Only {device} is updated. Your other screens keep playing." if others else "",
        ("Everyone is signed out when it restarts. You will need the password to sign in again."
         if auth.has_credentials() else ""),
        f"If anything goes wrong, {device} stays on the version it has now, or goes back to it by itself.",
    ]
    return _confirm_card(f"Update {device} now?",
                         "Best done when nobody needs the screen. If you update now:",
                         bullets, "Not now", "/", "Yes, update now", "/api/update", danger=False,
                         onsubmit="startUpdate();return false;")


def _password_clear_body(cfg: dict) -> str:
    """Before the panel is opened to anyone on the network."""
    device = _esc(cfg["name"])
    if not auth.has_credentials():
        return _nothing_card("This panel has no password", "There is nothing to remove.")
    bullets = [
        "Anyone on your network can open this panel and change its content, settings and pairing.",
        "Anyone could also set their own password and lock you out.",
        "Your screens keep playing.",
        "To lock it again, open Settings and choose &ldquo;Lock this panel&rdquo;.",
    ]
    return _confirm_card(f"Remove the password from {device}?",
                         f"Right now only people with the password can change content, settings and "
                         f"pairing on {device}. If you remove it:",
                         bullets, "Keep the password", "/", "Yes, remove the password",
                         "/api/password/clear")


def _display_here_off_body(cfg: dict, device_id: str) -> str:
    """Before a controller's own screen stops playing the playlist."""
    device = _esc(cfg["name"])
    shown = _display_names(pairing.list_displays())
    one = len(shown) == 1
    others = (f"{_names(shown)} {'keeps' if one else 'keep'} playing. Nothing changes on "
              f"{'it' if one else 'them'}." if shown else "")
    back_on = ("Nothing is deleted. To turn it back on, open Settings and tick "
               "&ldquo;Show content on this screen&rdquo;.")
    if sync.items_for_display(library.list_items(), device_id):
        lead = f"Right now {device}&rsquo;s own screen plays the playlist. If you turn this off:"
        first = f"{device}&rsquo;s screen stops playing and shows {_WAITING}"
    else:
        lead = f"{device}&rsquo;s own screen has nothing to play right now. If you turn this off:"
        first = "It won&rsquo;t play anything you add later either, until you turn this back on."
    return _confirm_card(f"Stop showing content on {device}&rsquo;s own screen?", lead,
                         [first, others, back_on], "Keep showing content", "/", "Yes, turn it off",
                         "/api/display-here")


def _repair_body(cfg: dict) -> str:
    """Before a paired display puts up a pairing code for a different controller."""
    controller = pairing.get_controller() or {}
    ctl = _esc(controller.get("name") or "its controller")
    bullets = [
        "Its TV shows a big pairing code instead of its content, for up to 3 minutes.",
        f"The first controller to enter the code takes over this screen. If that isn&rsquo;t {ctl}, "
        f"{ctl} can no longer send to it, and every Push from {ctl} will fail.",
        "If nobody enters the code, nothing changes. You can also press &ldquo;Cancel pairing&rdquo; on Home.",
    ]
    return _confirm_card("Re-pair this screen?",
                         f"This screen is paired to {ctl}. If you go on:", bullets,
                         "Not now", "/", "Yes, show a pairing code", "/api/pair/start",
                         closing=(f"Only want to change what this screen shows? Change it on {ctl} "
                                  f"instead. This screen updates by itself.{_go_to_controller(controller)}"))


def _promote_body(cfg: dict) -> str:
    """Before the PowerPoint conversion software is downloaded and installed."""
    device = _esc(cfg["name"])
    if promote.has_conversion_tools():
        return _nothing_card("PowerPoint support is already installed", "There is nothing to install.")
    state = promote.conversion_state()
    if state.get("state") == "installing":
        return _nothing_card("PowerPoint support is being installed", _esc(state.get("detail", "")))
    bullets = [
        "It downloads from the internet and takes a while.",
        "Your screens keep playing while it installs.",
        "If there is no internet, it stops. You can try again later.",
        ("The last try didn&rsquo;t finish. Trying again is safe."
         if state.get("state") == "failed" else ""),
    ]
    return _confirm_card(f"Install PowerPoint support on {device}?",
                         f"{device} needs extra software to turn PowerPoint files into slides. If you install it:",
                         bullets, "Not now", "/", "Yes, install it", "/api/promote",
                         closing="Google Slides, web pages, images and videos work without it.",
                         danger=False)


def _unpair_confirm_body() -> str:
    """What a person sees before unpairing a display from its controller.

    Unpairing used to sit behind the browser's one-line popup ("Unpair this screen and
    remove the pushed content?"), right beside a Re-pair button. It was easy to click
    through, and it never said what actually breaks: the controller can no longer send
    to this screen, and every Push fails until someone pairs it again. On a church
    screen that is exactly what happened - someone swapping the week's announcements
    on the display itself unpaired it, and the controller's pushes failed for days.
    So this spells out the consequence, makes "keep it paired" the obvious choice, and
    points at the place the content should really be changed: the controller."""
    controller = pairing.get_controller()
    if not controller:
        return _nothing_card("This screen isn't paired", "There is nothing to unpair.")
    name = _esc(controller.get("name") or "the controller")
    bullets = [
        f"<b>{name} can no longer send anything to this screen.</b> Every Push to it will "
        f"fail until it is paired again.",
        f"What {name} sent is taken off this screen straight away.",
        (f"It plays its own {_count(len(library.list_items()), 'item')} instead."
         if library.list_items() else f"It will show {_WAITING}"),
        f"To pair it again, press &ldquo;Start pairing&rdquo; on this screen&rsquo;s Home. Then on "
        f"{name}, use &ldquo;Add by address&rdquo; and enter the code.",
    ]
    return _confirm_card(f"Unpair this screen from {name}?",
                         f"This screen is paired to <b>{name}</b>. If you unpair it:", bullets,
                         "Keep it paired", "/", "Yes, unpair this screen", "/api/pair/forget",
                         closing=(f"Only want to change what this screen shows? Change it on "
                                  f"<b>{name}</b> instead &mdash; this screen updates by itself."
                                  f"{_go_to_controller(controller)}"))


def _page(title: str, role: str, cfg: dict, body: str, active: str = "home") -> HTMLResponse:
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} · eximaro</title>
{_THEME_HEAD}
{_FONTS_HEAD}
<style>{_CSS}</style></head>
<body>
  <main class="stage">
    {_header(cfg, role)}
    {_nav(active, role)}
    {_update_alert_banner()}
    {body}
  </main>
  {_settings_drawer(cfg, role)}
  {_WORKING_HTML}
  {_SETTINGS_JS}
</body></html>"""
    return HTMLResponse(html)


# Wi-Fi pages are served on the setup network too, which has NO internet, so each one
# is fully self-contained: no external CSS, web fonts or favicon. Anything the browser
# has to fetch separately can hang there and leave the page blank. System fonts and
# inline colours keep it to a single request.
_WIFI_STYLE = """
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;max-width:520px;margin:0 auto;
  padding:28px 22px;background:#F8F9FA;color:#17191C;-webkit-font-smoothing:antialiased;overflow-wrap:anywhere}
.wm{font-weight:700;font-size:1.2rem;letter-spacing:-.01em;margin-bottom:18px}
.wm .x{color:#2F7FE0}
h1{font-weight:700;font-size:1.4rem;letter-spacing:-.01em;margin:0 0 12px}
h2{font-weight:700;font-size:1.05rem;margin:22px 0 6px}
label{font-weight:600;font-size:.85rem;color:#5E646B;display:block;margin-top:8px}
input,button{font:inherit;padding:12px;width:100%;box-sizing:border-box;margin:6px 0;border-radius:10px;
  border:1.5px solid #E6E8EA;background:#fff;color:#17191C}
input:focus{outline:none;border-color:#2F7FE0;box-shadow:0 0 0 3px #E3F0FC}
button{background:#17191C;color:#fff;font-weight:600;border:0;cursor:pointer;margin-top:10px}
.stop{background:#fff;color:#5E646B;border:1px solid #E6E8EA}
.danger{background:#fff;color:#C93B3B;border:1.5px solid #C93B3B}
.ok{color:#1E7A4E;font-weight:600}.err{color:#C93B3B;font-weight:600}
.note{color:#5E646B;font-size:.92rem}
ul{padding-left:20px;color:#3A3F45}li{margin:7px 0}
img{background:#fff;padding:8px;border-radius:12px;display:block;border:1px solid #E6E8EA}
"""


def _wifi_shell(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · eximaro</title>
<style>{_WIFI_STYLE}</style></head><body>
<div class="wm">e<span class="x">x</span>imaro</div>
{body}
</body></html>"""


def _wifi_move_screen(cfg: dict, ssid: str, password: str, here: str = "") -> HTMLResponse:
    """The screen before sending a device to a Wi-Fi network from the normal network.

    It says only what is certain. A device on Wi-Fi leaves that network at once, so a page
    open on its Wi-Fi address stops answering; a network cable, when there is one, keeps
    it reachable. Whether a failed join finds its way back by itself depends on the
    networks around it, so the screen doesn't promise it. `here` is the address this page
    was reached on."""
    name, new = _esc(cfg["name"]), _esc(ssid)
    real = [a for a in discovery.labeled_ips() if not a["ip"].startswith(discovery.AP_SUBNET)]
    cable = [a["ip"] for a in real if a["label"] == "Ethernet"]
    on_wifi = any(a["label"] == "Wi-Fi" for a in real)
    current_ssid = wifi.status().get("ssid") if on_wifi else None
    if cable and not on_wifi:
        title, lead = f"Connect {name} to {new}?", "If you continue:"
        displays = pairing.list_displays() if cfg.get("role") == "controller" else []
        if displays:
            # Once it has Wi-Fi it shows and shares its Wi-Fi address first, and that
            # is where its displays are told to fetch pictures and videos from.
            need = "needs" if len(displays) == 1 else "need"
            bullets = ["It stays on its network cable as well.",
                       f"Once it&rsquo;s on {new}, {_names(_display_names(displays))} {need} to "
                       f"be able to reach {new} too. If they can&rsquo;t, the next change you "
                       "send makes them drop their pictures and videos and keep only web "
                       "links."]
        else:
            bullets = ["It stays on its network cable as well, so nothing it&rsquo;s doing now stops."]
        bullets.append(f"If the name or password is wrong, it simply won&rsquo;t join {new}.")
        keep, yes, yes_class = "Cancel", f"Yes, connect to {new}", "stop"
    else:
        cur = f"<b>{_esc(current_ssid)}</b>" if current_ssid else "the network it&rsquo;s on now"
        title = f"Move {name} to {new}?"
        lead = "Check the network name and password first. If you continue:"
        if cable:
            link = f"http://{cable[0]}:{config.PORT}"
            wired = f'<a href="{_esc(link)}">{_esc(link)}</a>'
            bullets = [f"{name} leaves {cur} right away"
                       + (". This page stays open: it reached the screen over its network cable."
                          if here in cable else ", and this page stops answering here."),
                       f"Once it&rsquo;s on {new}, open its page from a phone or computer on {new}.",
                       f"Its network cable keeps it reachable at {wired}, even if it can&rsquo;t join {new}."]
        else:
            bullets = [f"{name} leaves {cur} right away, and this page stops answering here.",
                       f"Once it&rsquo;s on {new}, open its page from a phone or computer on {new}.",
                       f"If it can&rsquo;t join {new} &mdash; a wrong password, or out of range &mdash; "
                       f"it may not be reachable until it&rsquo;s back on {cur}. Plugging in a "
                       "network cable brings it back."]
        # A controller reaches its displays over the network they share (on a device with
        # both, that is its Wi-Fi address: the one it shows and pairs with).
        role = cfg.get("role")
        controller = pairing.get_controller() if role == "display" else None
        displays = pairing.list_displays() if role == "controller" else []
        if controller:
            ctl = _esc(controller.get("name") or "Its controller")
            bullets.append(f"{ctl} can only send it content while they&rsquo;re on the same network.")
        elif displays:
            verb = "gets" if len(displays) == 1 else "get"
            bullets.append(f"{_names(_display_names(displays))} only {verb} content while "
                           "on the same network as this controller.")
        keep = f"Keep it on {_esc(current_ssid)}" if current_ssid else "Keep it where it is"
        yes, yes_class = f"Yes, move it to {new}", "danger"
    points = "".join(f"<li>{b}</li>" for b in bullets)
    body = f"""<h1>{title}</h1>
<p>{lead}</p>
<ul>{points}</ul>
<form method="get" action="/"><button type="submit">{keep}</button></form>
<form method="post" action="/api/wifi/connect">
  <input type="hidden" name="confirm" value="yes">
  <input type="hidden" name="ssid" value="{new}">
  <input type="hidden" name="password" value="{_esc(password)}">
  <button class="{yes_class}" type="submit">{yes}</button>
</form>"""
    # The password rides along in the yes form, so this page must never be kept.
    return HTMLResponse(_wifi_shell("WiFi setup", body), headers={"Cache-Control": "no-store"})


# The network NetworkManager gives the setup hotspot (gateway wifi.AP_GATEWAY).
_SETUP_NETWORK = ipaddress.ip_network("10.42.0.0/24")


def _from_this_device(request: Request) -> bool:
    """Did this request come from the device itself - the kiosk browser on the TV?"""
    try:
        ip = ipaddress.ip_address(request.client.host if request.client else "")
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _splash(cfg: dict) -> HTMLResponse:
    return HTMLResponse(_SETUP_HTML.replace("__DEVICE_NAME__", _esc(cfg["name"])))


def _login_page(cfg: dict, error: str = "") -> HTMLResponse:
    """The sign-in screen shown when the panel is locked. Includes the offline
    recovery command so a forgotten password is never a dead end."""
    err = f'<p class="loginerr">{_esc(error)}</p>' if error else ""
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · eximaro</title>
{_THEME_HEAD}
{_FONTS_HEAD}
<style>{_CSS}
  .loginwrap{{min-height:78vh;display:flex;align-items:center;justify-content:center}}
  .logincard{{width:100%;max-width:380px;margin:0}}
  .logincard .fld{{margin-bottom:10px}}
  .logincard .fld input{{width:100%}}
  .loginerr{{background:var(--danger-soft);border:1px solid #EBC9C9;color:var(--danger);
             padding:10px 12px;border-radius:10px;font-size:.9rem;margin:0 0 12px}}
  .full{{width:100%;justify-content:center;display:flex}}
  .cmd{{background:var(--ink);color:var(--solid-fg);padding:10px 12px;border-radius:10px;
        font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.72rem;white-space:pre-wrap;
        word-break:break-all;margin:8px 0 0}}
</style></head>
<body>
  <main class="stage">
    <div class="loginwrap">
      <div class="card logincard">
        <span class="brand">e<span class="x">x</span>imaro</span>
        <h1 style="margin:14px 0 4px">Sign in</h1>
        <p class="lead">Enter the control-panel password for
          <b>{_esc(cfg['name'])}</b>.</p>
        {err}
        <form method="post" action="/login">
          <label class="fld">Username
            <input name="username" autocomplete="username" required autofocus>
          </label>
          <label class="fld">Password
            <input name="password" type="password" autocomplete="current-password" required>
          </label>
          <button class="btn-primary full" type="submit">Sign in</button>
        </form>
        <details style="margin-top:16px">
          <summary>Forgot the password?</summary>
          <p class="hint" style="margin-top:8px">On the device itself (keyboard or
            SSH), run this once to remove the password and reopen the panel:</p>
          <pre class="cmd">sudo -u eximaro /opt/eximaro/.venv/bin/python -m app reset-password</pre>
        </details>
      </div>
    </div>
  </main>
</body></html>"""
    return HTMLResponse(html)


def _fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _health_body(cfg: dict, role: str, device_id: str, started: float) -> str:
    """Plain-language status + the recent activity log for this device."""
    url = f"http://{discovery.primary_ip()}:{config.PORT}"
    rows = []

    def row(key, value):
        rows.append(f'<div class="hrow"><span class="hk">{_esc(key)}</span>'
                    f'<span class="hv">{value}</span></div>')

    row("Status", '<span class="ok-dot"></span> Running')
    row("Name", _esc(cfg.get("name") or "—"))
    row("Role", _esc((role or "unset").capitalize()))
    row("Device ID", f'<span class="mono">{_esc(device_id[:12])}…</span>')
    row("Address", f'<a href="{_esc(url)}">{_esc(url)}</a>')
    # The packaged version string has read 0.0.1 on every build ever made, so it
    # can't answer "is this screen on the current code?". The source stamp can.
    row("Software", f'<span class="mono">{_esc(config.VERSION)}</span>')
    row("Uptime", _esc(_fmt_uptime(time.time() - started)))

    if role == "controller":
        row("Paired displays", str(len(pairing.list_displays())))
        row("Playlist items", str(len(library.list_items())))
        row("PowerPoint support", _esc(promote.conversion_state()["state"]))
    else:
        pushed = sync.screen_items()
        if pushed is not None:
            row("Now showing", f"{len(pushed)} item(s) sent from a controller")
        else:
            n = len(library.list_items())
            row("Now showing", f"{n} local item(s)" if n else "nothing yet")
        ctrl = pairing.get_controller() if pairing.is_claimed() else None
        row("Paired to", _esc(ctrl.get("name") or "a controller") if ctrl else "not paired")

    status_card = '<div class="card"><h2>This device</h2>' + "".join(rows) + "</div>"

    events = activity.recent(40)
    if events:
        log_rows = "".join(
            '<div class="hrow"><span class="hk mono">' + _esc(e.get("when", "")) + "</span>"
            '<span class="hv">' + _esc(e.get("event", ""))
            + (f' <span class="muted">— {_esc(e.get("detail"))}</span>' if e.get("detail") else "")
            + "</span></div>"
            for e in events
        )
    else:
        log_rows = '<p class="empty">No activity yet.</p>'
    log_card = '<div class="card"><h2>Recent activity</h2>' + log_rows + "</div>"

    intro = ('<p class="eyebrow">Status &amp; activity</p><h1>Health</h1>'
             '<p class="lead">A quick look at what this device is doing.</p>')
    return intro + status_card + log_card


def _targets_control(it: dict, displays) -> str:
    """Per-item 'which screens' control, shown on a controller with paired displays.
    No box checked = every screen; otherwise just the chosen displays."""
    if not displays:
        return ""
    tgt = it.get("targets") or []
    boxes = ""
    for d in displays:
        did = d["device_id"]
        chk = " checked" if did in tgt else ""
        boxes += (f'<label class="tgtbox"><input type="checkbox" name="display" value="{_esc(did)}"'
                  f'{chk} onchange="this.form.submit()"> {_esc(d.get("name") or did[:8])}</label>')
    summ = "All screens" if not tgt else f"{len(tgt)} screen(s)"
    return (f'<details class="tgt"><summary>On: {summ}</summary>'
            f'<form method="post" action="/api/content/targets">'
            f'<input type="hidden" name="item_id" value="{it["id"]}">'
            f'<p class="thint">Leave all unchecked to show on every screen.</p>'
            f'{boxes}</form></details>')


def _content_body(cfg: dict, displays=None) -> str:
    """Content management shared by both roles: add URL/Slides, upload, and the
    playlist with per-item ordering, timing, and (on a controller) which screens."""
    items = library.list_items()
    if items:
        rows = ""
        last = len(items) - 1
        for n, it in enumerate(items):
            up_dis = " disabled" if n == 0 else ""
            dn_dis = " disabled" if n == last else ""
            is_gslides = it["type"] == "url" and it.get("slides")
            is_youtube = it["type"] == "url" and it.get("youtube")
            is_show = it["type"] == "slideshow"
            is_video = it["type"] == "video"
            is_slides_raw = (it["type"] == "url" and not it.get("slides")
                             and library._is_google_slides(it.get("ref", "")))
            # Still being sized by the headless browser. Time-boxed on purpose: if the
            # measurement died without clearing its marker, the row quietly falls back
            # to the manual re-check below instead of spinning for ever.
            is_measuring = is_slides_raw and library.measuring(it)
            secs_form = f"""
                <form class="secs" method="post" action="/api/content/seconds">
                  <input type="hidden" name="item_id" value="{it['id']}">
                  <input name="seconds" type="number" min="{library.MIN_SECONDS}" value="{it['seconds']}"
                         title="{{title}}" aria-label="{{title}}">
                  <button class="btn-ghost set" title="Save time">Set</button>
                </form>"""
            if is_gslides:
                note = (f'<span class="slidenote">&#9654; plays all {it["slides"]} slides &middot; '
                        f'{it.get("per_slide", "?")}s each (from the link)</span>')
                secs_html = '<span class="secs auto" title="Timed automatically from the deck">auto</span>'
            elif is_youtube:
                note = '<span class="slidenote">&#9654; YouTube &middot; plays the whole video</span>'
                secs_html = ('<span class="secs auto" title="Plays the whole video, then moves on">'
                             'auto</span>')
            elif is_show:
                note = f'<span class="slidenote">&#9654; {it["slides"]}-slide show &middot; {it["seconds"]}s each</span>'
                secs_html = secs_form.replace("{title}", "Seconds per slide")
            elif is_video:
                note = '<span class="slidenote">&#9654; video' + ('' if it["seconds"] else ' (plays in full)') + '</span>'
                secs_html = f"""
                <form class="secs" method="post" action="/api/content/seconds">
                  <input type="hidden" name="item_id" value="{it['id']}">
                  <input name="seconds" type="number" min="0" value="{it['seconds']}"
                         title="Seconds (0 = play the whole video)" aria-label="Seconds (0 = play the whole video)">
                  <button class="btn-ghost set" title="Save time">Set</button>
                </form>"""
            elif is_measuring:
                note = ('<span class="slidenote measuring"><span class="dot"></span>'
                        'Reading the deck to time it automatically&hellip; '
                        'this takes a few seconds</span>')
                # No re-check button while it is already running — offering it would
                # invite a second headless browser on top of the first. The manual
                # seconds box stays, so nobody is ever stuck waiting.
                secs_html = secs_form.replace("{title}", "Or set the seconds by hand")
            elif is_slides_raw:
                # If Google is refusing anonymous viewers, say so — that is the actual
                # reason the length could not be read, and nothing on the device fixes it.
                # Only the last verdict is read: a page view never asks Google itself.
                _ok, _why = net.last_probe(it.get("ref", "")) or (True, "")
                if not _ok and "sign in" in _why:
                    note = (f'<span class="slidenote">&#9654; Google Slides &mdash; {_esc(_why)}. '
                            'Check File &rarr; Share &rarr; Publish to web, and that your Google '
                            'account allows publishing to anyone</span>')
                else:
                    note = ('<span class="slidenote">&#9654; Google Slides &mdash; haven\'t read its '
                            'length yet; re-check while online, or set the seconds by hand. If it '
                            'never reads, check File &rarr; Share &rarr; Publish to web</span>')
                secs_html = (f"""
                <form class="secs" method="post" action="/api/content/remeasure"
                      title="Read the deck length now (needs to be online)">
                  <input type="hidden" name="item_id" value="{it['id']}">
                  <button class="btn-ghost set">Re-check length</button>
                </form>"""
                    + secs_form.replace("{title}", "Or set seconds to cover the whole deck"))
            else:
                note = ""
                secs_html = secs_form.replace("{title}", "Seconds on screen")
            targets_html = _targets_control(it, displays)
            # Surface the URL (for links) or file name (for uploads) so items that
            # share a title — several "Google Slides", say — are easy to tell apart.
            # A plain web page is already named by its URL, so don't repeat it.
            ref = str(it.get("ref", ""))
            is_link = ref.startswith(("http://", "https://"))
            source = ref if (is_link and ref != it["name"]) else ""
            src_html = (
                f'<span class="srcrow"><span class="src" title="{_esc(source)}">{_esc(source)}</span>'
                f'<button type="button" class="copy" data-copy="{_esc(source)}" '
                f'onclick="copyUrl(this)">Copy</button></span>'
                if source else "")
            # Sound (videos + YouTube) and captions (YouTube) toggles — a muted screen
            # is the norm, so sound is off by default; captions are on by default. Each
            # checkbox saves on change, like the shuffle control.
            toggles = ""
            if is_youtube or is_video:
                on = " checked" if it.get("sound") else ""
                toggles += (
                    f'<form class="mtog" method="post" action="/api/content/sound">'
                    f'<input type="hidden" name="item_id" value="{it["id"]}">'
                    f'<label><input type="checkbox" name="sound" value="on"{on} '
                    f'onchange="this.form.submit()"> Play sound</label></form>')
            if is_youtube:
                on = " checked" if it.get("cc", True) else ""
                toggles += (
                    f'<form class="mtog" method="post" action="/api/content/cc">'
                    f'<input type="hidden" name="item_id" value="{it["id"]}">'
                    f'<label><input type="checkbox" name="cc" value="on"{on} '
                    f'onchange="this.form.submit()"> Captions</label></form>')
            toggles_html = f'<span class="mtogs">{toggles}</span>' if toggles else ""
            rows += f"""
              <div class="item">
                <span class="ord">
                  <form method="post" action="/api/content/move">
                    <input type="hidden" name="item_id" value="{it['id']}">
                    <input type="hidden" name="direction" value="up">
                    <button class="mv" title="Move up" aria-label="Move up"{up_dis}>&#9650;</button>
                  </form>
                  <form method="post" action="/api/content/move">
                    <input type="hidden" name="item_id" value="{it['id']}">
                    <input type="hidden" name="direction" value="down">
                    <button class="mv" title="Move down" aria-label="Move down"{dn_dis}>&#9660;</button>
                  </form>
                </span>
                <span class="name"><span class="nm" title="{_esc(it['name'])}">{_esc(it['name'])}</span>{targets_html}</span>
                {secs_html}
                <form method="get" action="/content/remove">
                  <input type="hidden" name="item_id" value="{it['id']}">
                  <button class="x" title="Remove&hellip;" aria-label="Remove {_esc(it['name'])}">&times;</button>
                </form>
                {note}{src_html}{toggles_html}
              </div>"""
        playlist_inner = rows
    else:
        playlist_inner = ('<p class="empty">Nothing yet — add a link or upload '
                          'something below.</p>')

    shuffle_checked = " checked" if cfg.get("shuffle") else ""
    shuffle_row = f"""
        <form method="post" action="/api/playback" class="shuffle">
          <label><input type="checkbox" name="shuffle" value="on"{shuffle_checked}
            onchange="this.form.submit()"> Shuffle order</label>
          <span class="hint" style="margin:0">Off (default) plays the list top to bottom, in order.</span>
          <noscript><button class="btn-ghost" type="submit">Save</button></noscript>
        </form>"""

    return f"""
      <div class="card">
        <h2>Add a web page, Google Slides, or video {_tip('slides')}</h2>
        <p class="hint">Paste a web address, a Google Slides &ldquo;Publish to web&rdquo;
          link, a YouTube link, or a direct video link.</p>
        <form method="post" action="/api/content/url" class="row" onsubmit="showWorking('Adding the link…')">
          <input name="url" placeholder="https://…  ·  Google Slides link  ·  YouTube link" required>
          <input name="seconds" type="number" min="{library.MIN_SECONDS}" value="15" title="seconds on screen">
          <button class="btn-primary" type="submit">Add</button>
        </form>
        <p class="hint tail">A Google Slides deck plays all the way through
          automatically: it reads the slide count and the per-slide time from your
          &ldquo;Publish to web&rdquo; link (the device needs internet the moment you
          add it). Turn on auto-advance when you publish so the link carries the timing.
          A YouTube link plays the whole video and then moves on &mdash; the seconds
          box doesn&rsquo;t apply to it.</p>
      </div>

      <div class="card">
        <h2>Upload an image, PowerPoint, or video</h2>
        <p class="hint">A PowerPoint becomes <b>one</b> slideshow that plays all its
          slides in order; a video plays in full. The seconds box is how long each
          image or slide stays up.</p>
        <form method="post" action="/api/content/upload" enctype="multipart/form-data" class="row" onsubmit="showWorking('Uploading…')">
          <input name="file" type="file" accept="image/*,video/*,.pptx,.ppt" required>
          <input name="seconds" type="number" min="{library.MIN_SECONDS}" value="10" title="seconds per image or slide">
          <button class="btn-primary" type="submit">Upload</button>
        </form>
      </div>

      <div class="card">
        <h2>Playlist {_tip('playlist')}</h2>
        <p class="hint">Plays in this order, top to bottom. Use the arrows to
          reorder, and set how long each item stays on screen.</p>
        {shuffle_row}
        {playlist_inner}
      </div>
      <script>
      // A deck added a moment ago is still being timed by a headless browser, which
      // finishes AFTER this page was sent. Watch for it and refresh once, so the row
      // turns into "auto" by itself. Without this the page kept saying "length not
      // read yet" until someone reloaded or pressed Re-check, which is what made the
      // automatic timing look like it wasn't working at all.
      (function(){{
        if(!document.querySelector('.slidenote.measuring')) return;
        var tries=0;
        var iv=setInterval(async function(){{
          tries++;
          try{{
            var s=await (await fetch('/api/content/measuring',{{cache:'no-store'}})).json();
            if(!s||s.pending===0){{clearInterval(iv);location.reload();return;}}
          }}catch(e){{}}
          // Longer than the server's own grace period, so the reload lands once the
          // row has already fallen back to the manual re-check and we don't re-arm.
          if(tries>72){{clearInterval(iv);location.reload();}}
        }},2500);
      }})();
      </script>
    """


def _display_home(cfg: dict) -> HTMLResponse:
    code = pairing.current_code()
    if code:
        section = f"""
          <div class="card">
            <h2>Pairing {_tip('pairing')}</h2>
            <p class="hint">On your controller, tap &ldquo;Find displays&rdquo;, then enter this code next to this display:</p>
            <div class="code">{_esc(code)}</div>
            <div class="status"><span class="pulse"><i></i></span>
              Waiting to connect · valid for 3 minutes. It also shows on the screen itself.</div>
            <form method="post" action="/api/pair/cancel">
              <button class="btn-danger" type="submit">Cancel pairing</button>
            </form>
          </div>"""
    elif pairing.is_claimed():
        # Top of a paired screen: who controls it and where, and where to go to change
        # what it shows - nothing to click here that could cut it off. The name and
        # address are refreshed by every verified push (pairing.refresh_controller),
        # so they are the controller's real ones today, renamed or not.
        controller = pairing.get_controller() or {}
        c_name = _esc(controller.get("name") or "its controller")
        c_addr = pairing.lan_address(controller.get("address"))
        where = f' at <span class="mono">{_esc(c_addr)}</span>' if c_addr else ""
        go_there = (f'<a href="http://{_esc(c_addr)}:{config.PORT}/content">open '
                    f'{c_name}&rsquo;s Content page</a>' if c_addr else f"use {c_name}")
        section = f"""
          <div class="card">
            <h2>Controlled by {c_name} {_tip('pairing')}</h2>
            <p class="hint">This screen shows what <b>{c_name}</b>{where} sends it.
              To change what&rsquo;s on it, {go_there}.</p>
            <p class="hint">Pairing options are in Settings.</p>
          </div>"""
    else:
        section = f"""
          <div class="card">
            <h2>Pair to a controller {_tip('pairing')}</h2>
            <p class="hint">Start pairing, then enter the code it shows on your controller.</p>
            <form method="post" action="/api/pair/start">
              <button class="btn-primary" type="submit">Start pairing</button>
            </form>
          </div>"""

    # When a controller has pushed a playlist, let the operator take it down from
    # the display itself — important if the controller is offline and can't. It sits
    # BELOW the content, not at the top: at the top it read as the first thing to try
    # when a screen looked stuck, and people reached for it (or Unpair) to "fix" one.
    pushed = sync.screen_items()
    pushed_card = ""
    if pushed is not None:
        from_name = _esc((pairing.get_controller() or {}).get("name") or "its controller")
        pushed_card = f"""
          <div class="card note">
            <h2>Remove {from_name}&rsquo;s content from this screen</h2>
            <p class="hint">This screen is playing <b>{len(pushed)}</b> item(s) sent from
              {from_name}. Only remove it if {from_name} is offline and it has to come off
              this screen now &mdash; the next Push from {from_name} puts it back. To
              change what plays, change it on {from_name} instead.</p>
            <form method="get" action="/received/clear">
              <button class="btn-danger" type="submit">Remove pushed content&hellip;</button>
            </form>
          </div>"""

    # While a controller's content is playing, this screen's own list does not show -
    # say so, instead of inviting edits that change nothing on the wall.
    lead = ("It plays what its controller sends. Content added below only shows if that "
            "content is removed." if pushed is not None
            else "Pair it to a controller, or add content directly below.")
    intro = f"""
      <p class="eyebrow">This device shows content {_tip('display')}</p>
      <h1>Display</h1>
      <p class="lead">{lead}</p>
    """
    return _page("Display", "display", cfg, intro + section + _content_body(cfg) + pushed_card)


def _promote_banner(conv: dict) -> str:
    """A status note on the controller home about PowerPoint-conversion support,
    shown only when it isn't ready yet."""
    state = conv.get("state")
    if state == "ready":
        return ""
    detail = _esc(conv.get("detail", ""))
    if state == "installing":
        return (f'<div class="card note"><h2>Setting up PowerPoint support&hellip;</h2>'
                f'<p class="hint">{detail} Google Slides, web pages, and images work now.</p></div>')
    if state == "failed":
        return (f'<div class="card note warn"><h2>PowerPoint support didn\'t install</h2>'
                f'<p class="hint">{detail}</p>'
                f'<form method="get" action="/promote"><button class="btn-accent" type="submit">Try again&hellip;</button></form></div>')
    return (f'<div class="card note"><h2>PowerPoint support not installed</h2>'
            f'<p class="hint">{detail} You can still use Google Slides, web pages, and images.</p>'
            f'<form method="get" action="/promote"><button class="btn-accent" type="submit">Install PowerPoint support&hellip;</button></form></div>')


def _push_card() -> str:
    """The manual "send it now" card. It lives on the Content page, next to the
    playlist it sends — sending from a different tab than the one you edit on never
    made sense. Content changes already push on their own; this is the re-send for
    when a screen was off or unplugged at the time."""
    return f"""
      <div class="card">
        <h2>Push to all displays {_tip('push')}</h2>
        <p class="hint">Changes go out to your displays on their own. Use this to
          send the whole playlist again &mdash; handy if a screen was off or offline
          when you last changed something.</p>
        <button class="btn-accent" onclick="pushAll(this)">Push to all displays</button>
        <div id="pushResult" class="hint tail"></div>
      </div>
      <script>
      async function pushAll(btn){{
        const box=document.getElementById('pushResult');
        box.textContent='Sending…';
        if(btn) btn.disabled=true;
        try{{
          const r=await fetch('/api/push',{{method:'POST'}});
          const d=await r.json();
          if(d.message){{box.textContent=d.message;return;}}
          const ok=d.results.filter(x=>x.ok).length;
          const fail=d.results.filter(x=>!x.ok);
          box.textContent=`Sent to ${{ok}} display(s).`+(fail.length?` Failed: ${{fail.map(f=>f.name+(f.error?' — '+f.error:'')).join('; ')}}.`:'');
        }}catch(e){{box.textContent='Push failed.';}}
        finally{{if(btn) btn.disabled=false;}}
      }}
      </script>"""


def _control_home(cfg: dict) -> HTMLResponse:
    # The screens hub: the paired displays and finding new ones. Push moved to the
    # Content page (next to the playlist), and the library lives there too.
    banner = _promote_banner(promote.conversion_state())

    displays = pairing.list_displays()
    if displays:
        rows = ""
        for d in displays:
            rows += f"""
              <div class="item">
                <span class="name">{_esc(d['name'] or d['device_id'][:8])}</span>
                <span class="meta">{_esc(d['address'])}</span>
                <form method="get" action="/displays/remove">
                  <input type="hidden" name="device_id" value="{_esc(d['device_id'])}">
                  <button class="x" title="Unpair&hellip;" aria-label="Unpair {_esc(d['name'] or d['device_id'][:8])}">&times;</button>
                </form>
              </div>"""
        screens_inner = rows
    else:
        screens_inner = ('<p class="empty">No displays paired yet — '
                         'find one below.</p>')

    find = """
      <div class="card">
        <h2>Paired displays</h2>
        <div id="displays">__SCREENS__</div>
      </div>

      <div class="card">
        <h2>Find displays <span class="tip" data-tip="__PAIR_TIP__" aria-label="More info">i</span></h2>
        <p class="hint">Scan the network for displays that are ready to pair.</p>
        <button class="btn-primary" onclick="findDisplays()">Find displays</button>
        <div id="found" class="hint tail">Tap &ldquo;Find displays&rdquo; to scan the network.</div>
        <details style="margin-top:14px">
          <summary>Add by address (fallback)</summary>
          <form method="post" action="/api/displays/add" class="row">
            <input name="address" placeholder="192.168.1.50" required>
            <input name="port" type="number" value="8080">
            <input name="code" placeholder="CODE" required style="width:120px">
            <button class="btn-ghost" type="submit">Pair</button>
          </form>
        </details>
      </div>
      <script>
      // Names come from whatever answers on the network, so they go in as text.
      const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      async function findDisplays(){
        const box=document.getElementById('found');
        box.textContent='Scanning…';
        try{
          const r=await fetch('/api/discover');
          const d=await r.json();
          const list=(d.devices||[]).filter(x=>x.role!=='controller');
          if(!list.length){box.textContent='No displays found. Start pairing on the display, or use Add by address.';return;}
          box.innerHTML=list.map(x=>`<div class="item">
            <span class="name">${esc(x.name||x.address)}</span><span class="meta">${esc(x.address)}</span>
            ${x.paired?'<span class="meta">paired</span>':
              `<form method="post" action="/api/displays/add" style="margin:0">
                 <input type="hidden" name="address" value="${esc(x.address)}">
                 <input type="hidden" name="port" value="${esc(x.port)}">
                 <input name="code" placeholder="CODE" required style="width:110px">
                 <button class="btn-primary" type="submit">Pair</button>
               </form>`}
          </div>`).join('');
        }catch(e){box.textContent='Scan failed.';}
      }
      </script>"""
    find = find.replace("__SCREENS__", screens_inner).replace("__PAIR_TIP__", _esc(_TIPS["pairing"]))

    intro = f"""
      <p class="eyebrow">This device runs the controls {_tip('controller')}</p>
      <h1>Screens</h1>
      <p class="lead">Manage which screens are connected. Build the playlist &mdash;
        and send it out &mdash; on the Content tab.</p>
    """
    # The playlist and its Push button both live on the Content page now; this page
    # is purely the screens hub — the paired displays and finding new ones to pair.
    return _page("Screens", "controller", cfg, intro + banner + find)


def _content_page(cfg: dict, device_id: str = "") -> HTMLResponse:
    """The content library on its own page (controller nav → Content). Targeting
    offers this controller's own screen plus every paired display."""
    role = cfg.get("role") or "display"
    displays = pairing.list_displays() if role == "controller" else []
    targetable = ([{"device_id": device_id, "name": cfg["name"] + " · this screen"}] + displays) if displays else []
    intro = ('<p class="eyebrow">Playlist &amp; media</p><h1>Content</h1>'
             '<p class="lead">Add links and uploads, then arrange the order and '
             'timing. Changes go out to your displays automatically.</p>')
    # Push sits at the top, under the heading: it's the action you come here to take
    # after an edit, and burying it under a long playlist meant scrolling to find it.
    # Only a controller with somewhere to send to gets the card — on a lone controller
    # or a display it would do nothing.
    push = _push_card() if (role == "controller" and displays) else ""
    return _page("Content", role, cfg, intro + push + _content_body(cfg, targetable), active="content")
