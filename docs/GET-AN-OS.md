# Get an operating system on the device

Your Pi or PC needs an operating system (the basic software that runs it) before eximaro. Below are the official guides, with the settings that matter called out. Follow the one section for your device.

## 🍓 Raspberry Pi

**You'll need:** a Pi 4 or 5, a microSD card (16 GB+), a card reader if your computer has no SD slot, the Pi's power supply, and a micro‑HDMI‑to‑HDMI cable (the Pi's HDMI port is the small kind).

1. On your everyday computer, install **Raspberry Pi Imager**: https://www.raspberrypi.com/software/
2. Open it. **Choose Device** → your Pi. **Choose OS** → *Raspberry Pi OS (other)* → **Raspberry Pi OS Lite (64‑bit)**. **Choose Storage** → your card.
3. Click **Next → Edit Settings** and set:
   - **Wi‑Fi** — your network name and password (so the Pi joins on its own).
   - A **username and password** — write them down.
   - **Services tab → Enable SSH** (password authentication).
4. Write it. Put the card in the Pi, plug in power, and turn it on.

Full walkthrough with pictures: https://www.raspberrypi.com/documentation/computers/getting-started.html

**Done →** [Install eximaro](./INSTALL.md)

## 💻 PC / laptop (Debian)

**You'll need:** a 64‑bit PC and an empty USB stick (4 GB+).

1. Download the small **netinst** image for **64‑bit PC (amd64)**: https://www.debian.org/distrib/
2. Write that `.iso` file to the USB stick with a flashing tool (such as balenaEtcher or Rufus).
3. Boot the PC from the USB stick — tap the boot‑menu key right after powering on (often **F12, Esc, or F2**).
4. Follow the installer. Choose **Debian 13**. At **Software selection**, uncheck the desktop, but keep **SSH server** and **standard system utilities**.

Full installation guide: https://www.debian.org/releases/stable/installmanual

**Done →** [Install eximaro](./INSTALL.md)

---

Every link here goes to the official **Raspberry Pi** (raspberrypi.com) or **Debian** (debian.org) site — the trusted sources. · [Home](../README.md)
