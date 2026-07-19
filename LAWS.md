# Project Goals

These are the goals that guide this project — the standard every design and
feature decision is measured against. They describe what it is meant to be, even
where the code hasn't fully caught up yet. The aim is simple: it should fully
work, and it should be nearly impossible to break.

### 1. Fully works, or it doesn't ship
A feature should be reliable enough to trust on a wall, unattended, for months —
or it waits. No "works most of the time." (This is why video and YouTube are
held back until they're solid, and why PowerPoint is turned into images rather
than rendered live.)

### 2. It can't break
The device should survive power cuts, network drops, and bad updates without ever
ending up wedged or corrupted. Worst case is "still running the last good
version," never "bricked."
- **Goal:** an immutable (read-only) system with A/B updates — two system copies;
  an update lands on the spare copy and is switched to only after it verifies,
  with automatic rollback to the last good version.
- **First step toward it:** staged-swap-with-rollback for the app, growing into
  the full A/B image.
- The app and the OS update separately, so they never tangle.

### 3. Dumb displays, smart center
Displays only ever show finished content. All the heavy work happens on the
controller. This keeps displays cheap, low-power, and able to run on the smallest
hardware.

### 4. Equal devices, one removable hat
Every device runs the same software. "Controller" is a role you turn on, not a
special box — and it can move to any device at any time, so nothing is stranded
if one device fails.

### 5. Trust anchored to the Device ID
A permanent Device ID is the root of trust. Names, hostnames, and IP addresses
can all change without breaking a paired link.

### 6. Never plaintext
No secret is ever stored or sent in plaintext — passwords are hashed, secrets
are encrypted, and commands are signed.

### 7. No command line for everyday users
If recovering from a normal problem needs a terminal, that's a design bug. Setup,
pairing, content, updates, and recovery should all be doable from a simple screen.

### 8. Always clear what's happening
Plain-language activity anyone can read, full technical logs for those who want
them, and one health screen that explains any problem in plain English.

---

## Planned features

Things we intend to build, kept here so they're not forgotten:

- **First-boot WiFi setup (no ethernet needed).** If a device has no wired
  connection, it starts its own temporary WiFi network and shows a setup page
  (a captive portal). The user connects from a phone, picks their WiFi, and
  enters the password; the device then joins that network. To honor goal #2, a
  device that ever loses its saved network falls back to this hotspot mode on its
  own, so it can always be reconnected from a phone — never a dead end.
- **Content groups and per-display targeting** (send to all, a group, or one).
- **Video and YouTube** once playback is genuinely smooth on the hardware.
