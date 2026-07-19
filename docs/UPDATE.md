# Update a device

Updating keeps everything — your content, your display‑to‑controller pairings, the device's role, and any password. Two ways:

## Quick update (app only)

From your phone or computer's browser — no terminal. On the controller's panel (`http://<device-ip>:8080`, your device's numbers), open **⚙ Settings → Software → Update now**. The screen restarts for a few seconds, then comes back updated.

Good for small app fixes. It does **not** refresh system parts like the WiFi‑setup feature — for those, use the full update.

## Full update (recommended)

Gets everything. Run it in a terminal **on the device itself** (over SSH, or with a keyboard plugged in):

```
sudo eximaro-update-full
```

It detects the role, downloads the latest, and reinstalls — keeping your data. **Done when** the scrolling stops at a fresh `$` prompt with no red error text.

If it says `command not found`, the device doesn't have the updater yet. Add it once (after this, future updates are just the line above):

```
sudo apt update
sudo apt install -y git
git clone https://github.com/eximaro/eximaro.git
cd eximaro
sudo ./install.sh --role display
```

Use `--role controller` for the controller. If it says `Permission denied`, run `sudo bash install.sh`.

## If something's off

- Can't download / offline → check the device's network, then run it again.
- Red error partway → your data is safe; just run it again (updates are safe to repeat).

Next: **[Install a new device](./INSTALL.md)** · **[Troubleshooting](./TROUBLESHOOTING.md)** · **[Get an OS](./GET-AN-OS.md)** · **[Home](../README.md)**
