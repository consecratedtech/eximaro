# Troubleshooting &amp; health checks

A field guide for checking a device and fixing the common problems. Most of it is copy‑paste commands, each with **what a healthy result looks like** and **what to do if it doesn't.**

## Two rules before you start

1. **The panel and the screen are two different things.** The **control panel** is the web page you open in a browser (`http://<device-ip>:8080`). The **screen** is what shows on the TV plugged into the device. When something's wrong, first work out *which* one — it cuts the problem in half. (Panel loads but TV is blank → it's the screen/kiosk. Panel won't load → it's the app or network.)
2. **You need a terminal on the device** for most of this — SSH in from your laptop (`ssh username@device-ip`) or plug in a keyboard. See [Install → step 1](./INSTALL.md#1-get-to-a-terminal-on-the-device) if you're not sure how. You're ready when you see a line ending in `$`.

Commands use the default paths and the name `eximaro`. `sudo` runs as administrator and may ask for your password.

---

## Start here — the 30‑second health check

```
curl -s localhost:8080/healthz; echo
systemctl is-active eximaro.service eximaro-kiosk.service
hostname -I
```

✅ **Healthy:** the first line prints `{"ok":true,"device_id":"…"}`; the second prints `active` twice; the third prints one or more IP addresses.

⚠️ If any line is wrong, jump to the matching section below.

---

## 1. The panel won't load

The app that serves the panel is **`eximaro.service`**.

```
systemctl status eximaro.service --no-pager
curl -s localhost:8080/healthz; echo
```

- ✅ **Good:** status shows `active (running)`, and `healthz` returns `{"ok":true,…}`.
- ⚠️ **`active` but the panel still won't open from your phone/laptop:** you're almost certainly on a **different network**, or typed the address without **`:8080`**. Confirm the device's address with `hostname -I` and that both are on the same Wi‑Fi/LAN.
- ⚠️ **`failed` / `activating (auto-restart)`:** read the log —
  ```
  journalctl -u eximaro.service -n 40 --no-pager
  ```
  Then run a full update (it re‑installs cleanly): `sudo eximaro-update-full`.

---

## 2. The TV screen is black, stuck, or flickering

The screen is drawn by the kiosk — **`eximaro-kiosk.service`** (the `cage` compositor running Chromium). If the **panel works in a browser but the TV is blank**, it's this.

**First, the two‑minute physical checks:**
- Give it **~60 seconds** — after any restart the screen shows a **"Starting up…"** splash before content.
- **TV** is powered on and set to the **HDMI input** the device is plugged into.
- **Raspberry Pi 5:** it drives the micro‑HDMI port **nearest the USB‑C power connector**. If the cable's in the other one, the TV stays black — move it.

**Then check the service:**

```
systemctl status eximaro-kiosk.service --no-pager
```

- ✅ **Good:** `active (running)`.
- ⚠️ **`activating (auto-restart)` with a rising restart counter, and the TV flickers between black and boot text:** the kiosk is **crash‑looping**. Get the real error (this strips the noisy login lines):
  ```
  journalctl -u eximaro-kiosk.service -n 50 --no-pager | grep -vi pam_unix
  ```
  What to look for:
  - **`status=134` and `chrome_crashpad_handler: --database is required`** → a known Chromium crash on freshly‑updated devices. **Fix:** run `sudo eximaro-update-full` (current versions include the fix), then reboot.
  - **`drm` / `seat` / `wlr` / `EGL` errors** → the compositor can't get the display. Confirm a screen is connected at boot, then reboot.
  - **`Permission denied` on a `/dev/dri…` device** → re‑run the installer to fix group membership: `sudo eximaro-update-full`.

---

## 3. The screen is up but shows no content (or the wrong thing)

Ask the device exactly what it's told to display:

```
curl -s localhost:8080/api/screen-data
```

Look at the very start of the output:

- ✅ **`"items":[ {…} ]`** — content *is* being served. If the TV still doesn't show it, it's a screen problem → section 2.
- ⚠️ **`"items":[]`** (empty) — nothing to play. Either the playlist is empty (add content in the panel), or every item is **targeted at other displays** (in the panel's Content page, set the items to **all screens**). On a **controller**, make sure **Settings → "Show content on this screen"** is checked.
- ⚠️ **`"pairing_code":"ABCD"`** (not null) — the device is a **display waiting to be paired**. Enter that code on the controller's panel to link it.
- ⚠️ **`"wifi_setup":{…}`** — the device has no working network and is showing its Wi‑Fi setup screen → section 5.

---

## 4. No sound

Sound rides HDMI through PipeWire. Check it **as the app user** (its audio lives in that user's session):

```
sudo -u eximaro env XDG_RUNTIME_DIR=/run/user/$(id -u eximaro) wpctl status
```

- ✅ **Good:** under **Sinks**, the line marked `*` (the default) is the **HDMI** one, and while a video with sound is playing you'll see a **`Chromium`** entry under **Streams** routed to it.
- ⚠️ **The `*` is on "Analog"/"Headphones," not HDMI:** the routing service didn't pick HDMI. Reboot, or re‑run `sudo eximaro-update-full`. (It matches the sink named "HDMI" / "DisplayPort" automatically.)
- ⚠️ **No `Chromium` stream at all while a video plays:** the video probably isn't set to play sound — in the panel, tick **"Play sound"** on that item. Also confirm the **TV isn't muted** and its volume is up. Sound is **off by default** on every item.

> Note: **YouTube needs internet every time it plays.** For sound/video that must survive an outage, **upload the video file** instead of linking YouTube.

---

## 5. Network &amp; connection problems

```
hostname -I
nmcli device status
ping -c 3 1.1.1.1
```

- ✅ **Good:** `hostname -I` shows an IP; `nmcli` shows a device `connected`; `ping` gets replies.
- ⚠️ **No IP / not connected:** plug in Ethernet, or use the on‑screen **Wi‑Fi setup** (the screen shows a QR to join the device's own network and a page to enter your Wi‑Fi). To drive Wi‑Fi from the terminal: `sudo eximaro-wifi scan` then set it from the panel.
- ⚠️ **`ping` fails but the device has an IP:** it's on the LAN but has **no internet**. Local content (uploaded images/video, PowerPoint) still plays fine; **YouTube and Google Slides won't** (they need internet).

---

## 6. Keep a Raspberry Pi current (firmware + system)

```
sudo rpi-eeprom-update
sudo apt update && apt list --upgradable
[ -f /var/run/reboot-required ] && echo "REBOOT NEEDED" || echo "no reboot pending"
```

- ✅ **Good:** `BOOTLOADER: up to date` (with matching `CURRENT`/`LATEST` dates); the upgradable list is empty; no reboot pending.
- ⚠️ **Bootloader update available:** `sudo rpi-eeprom-update -a && sudo reboot`.
- ⚠️ **Packages listed as upgradable:** `sudo apt full-upgrade -y`, then reboot if it asks.

> Don't use `rpi-update` — it flashes *test* firmware. `apt full-upgrade` + `rpi-eeprom-update` is the stable path.

---

## 7. Handy remote commands

Run these over SSH from anywhere you can reach the device.

| Do this | Command |
|---|---|
| Change what a device shows | `sudo eximaro-set-url "<link>" [seconds]` — a web page, Google Slides "Publish to web" link, or video. **Adds** by default; use `--replace` to clear the playlist first. |
| Full update (app + system parts) | `sudo eximaro-update-full` |
| Re‑check a device, change nothing | `sudo ./install.sh --check` (from the cloned `eximaro` folder) |
| Reset a forgotten panel password | `sudo -u eximaro /opt/eximaro/.venv/bin/python -m app reset-password` |
| Restart the app / the screen | `sudo systemctl restart eximaro.service` · `sudo systemctl restart eximaro-kiosk.service` |

---

## Where everything lives (reference)

| Thing | Location |
|---|---|
| Control panel | `http://<device-ip>:8080` |
| App service (serves the panel) | `eximaro.service` |
| Screen service (the TV) | `eximaro-kiosk.service` |
| Program code | `/opt/eximaro` (a link to the active release) |
| Data, content &amp; secrets | `/var/lib/eximaro` — locked down; view with `sudo ls -la /var/lib/eximaro` |
| Background helpers | `eximaro-update.path`, `eximaro-promote.path`, `eximaro-wifi.path` |

> **Advanced — screenshot the actual TV output** (when a picture is worth more than a description):
> ```
> sudo -u eximaro env XDG_RUNTIME_DIR=/run/user/$(id -u eximaro) \
>   WAYLAND_DISPLAY=$(sudo ls /run/user/$(id -u eximaro) | grep -m1 wayland) \
>   grim /tmp/screen.png
> ```
> Then copy `/tmp/screen.png` to your laptop with `scp`. (Only works while the kiosk is actually running.)

---

Next: **[Install a device](./INSTALL.md)** · **[Update a device](./UPDATE.md)** · **[Home](../README.md)**
