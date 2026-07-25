# Install eximaro (first time)

New device with no operating system yet? Do that first → **[Get an OS](./GET-AN-OS.md)**, then come back here.

Every device installs the same way. About 5 minutes.

## 1. Get to a terminal on the device

A **terminal** is a text window where you type commands. Two ways in:

- **Keyboard + screen on the device:** at the `login:` prompt, type the username and password you set with the OS. The password stays invisible as you type — that's normal.
- **From your laptop, over the network (SSH):** turn SSH on ([how](https://www.raspberrypi.com/documentation/computers/remote-access.html)), then run `ssh username@device-ip` from your laptop's terminal.

You're ready when you see a line ending in **`$`**.

## 2. Run the installer

Type these one line at a time — press **Enter** after each and wait for it to finish. They work on a fresh device; `apt` installs whatever's missing.

```
sudo apt update
sudo apt install -y git
git clone https://github.com/consecratedtech/eximaro.git
cd eximaro
sudo ./install.sh --role display
```

- **Setting up your controller?** Change `--role display` to `--role controller` on the last line. Use **controller** for the one device you'll manage everything from, **display** for every screen. One controller per network.
- `sudo` runs as administrator; it may ask for your password.
- If it says `Permission denied`, run `sudo bash install.sh` instead.

**Done when** the screen switches to full‑screen and the text ends with an address like `http://…:8080`. Write that address down.

## 3. Open the control panel

On a phone or computer **on the same network**, open that address in a browser — your device's own numbers, no `< >`:

```
http://192.168.1.50:8080
```

That's where you pick what to show.

## 4. Add more screens

Install each extra device the same way with `--role display`. Each shows a short **pairing code** on its screen — type it into the controller's panel to link them.

## If something's off

- `Permission denied` → `sudo bash install.sh`
- Re‑check a device → `sudo ./install.sh --check`
- Forgot the panel password → `sudo -u eximaro /opt/eximaro/.venv/bin/python -m app reset-password`
- Panel won't load → check both are on the same network, and the address is typed exactly (with `:8080`).
- Anything else (blank screen, no sound, no network) → **[Troubleshooting &amp; health checks](./TROUBLESHOOTING.md)**.

Next: **[Update a device](./UPDATE.md)** · **[Troubleshooting](./TROUBLESHOOTING.md)** · **[Home](../README.md)**
