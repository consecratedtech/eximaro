# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
"""LAN discovery over mDNS (zeroconf).

Each device advertises itself so a controller can find displays without anyone
reading IP addresses off a screen. Manual add-by-address stays available as a
fallback for networks where mDNS is blocked.
"""

import os
import socket
import subprocess
import time

from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf

SERVICE_TYPE = "_eximaro._tcp.local."


def _all_ipv4() -> list:
    """Every non-loopback, non-link-local IPv4 the OS has assigned. Works with no
    internet — essential for an offline appliance, where the route-to-8.8.8.8
    probe below can't pick a source address and would otherwise yield localhost."""
    found = []
    try:
        res = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2)
        for tok in res.stdout.split():
            parts = tok.split(".")
            if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                continue  # not a dotted-quad IPv4 (skips IPv6 and any stray tokens)
            if tok.startswith(("127.", "169.254.")):
                continue  # loopback / link-local
            if tok not in found:
                found.append(tok)
    except Exception:
        pass
    return found


# Virtual / non-physical interfaces we never want to hand out an address from.
_SKIP_IFACES = ("lo", "docker", "veth", "br-", "virbr", "vmnet", "tailscale", "zt")


def _iface_label(iface: str, wifi: bool) -> str:
    if wifi:
        return "Wi-Fi"
    if iface.startswith(("eth", "en")):
        return "Ethernet"
    return iface


def labeled_ips() -> list:
    """Every reachable IPv4 with a friendly label (Wi-Fi / Ethernet), Wi-Fi first.
    A phone scanning the QR is on Wi-Fi, so when a box has both we lead with Wi-Fi.
    Loopback, link-local, and virtual interfaces are left out, and the list is empty
    when the device has no real network address yet (so the screen can say so instead
    of showing a useless 127.0.0.1)."""
    out = []
    try:
        res = subprocess.run(["ip", "-o", "-4", "addr", "show"],
                             capture_output=True, text=True, timeout=2)
        for line in res.stdout.splitlines():
            parts = line.split()
            # "2: eth0    inet 192.168.1.5/24 brd ... scope global eth0"
            if len(parts) < 4 or parts[2] != "inet":
                continue
            iface, ip = parts[1], parts[3].split("/")[0]
            if iface.startswith(_SKIP_IFACES):
                continue
            if ip.startswith(("127.", "169.254.")):
                continue
            wifi = os.path.isdir(f"/sys/class/net/{iface}/wireless")
            out.append({"ip": ip, "iface": iface, "wifi": wifi,
                        "label": _iface_label(iface, wifi)})
    except Exception:
        return []
    out.sort(key=lambda e: (not e["wifi"], e["iface"]))  # Wi-Fi first, then by name
    seen, uniq = set(), []
    for e in out:
        if e["ip"] not in seen:
            seen.add(e["ip"])
            uniq.append(e)
    return uniq


# NetworkManager hands its hotspot the 10.42.x range. An address there means we're
# *hosting* the setup network, not connected to a real one — so it doesn't count.
AP_SUBNET = "10.42."


def hosting_ap() -> bool:
    """True while this device is hosting its own WiFi setup network."""
    return any(e["ip"].startswith(AP_SUBNET) for e in labeled_ips())


def real_ips() -> list:
    """The labeled addresses that are a genuine connection — everything except our
    own setup AP. Empty means nothing works and it's time to offer setup."""
    return [e for e in labeled_ips() if not e["ip"].startswith(AP_SUBNET)]


def lan_ips() -> list:
    """Addresses this device can be reached on, Wi-Fi first. Offline-safe."""
    labeled = labeled_ips()
    if labeled:
        return [e["ip"] for e in labeled]
    # Fallback for an environment without iproute2: the route probe, then hostname -I.
    ips = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))   # sends nothing; just selects a source IP
        ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            ips.append(ip)
    except OSError:
        pass
    finally:
        sock.close()
    for ip in _all_ipv4():
        if ip not in ips:
            ips.append(ip)
    return ips


def primary_ip() -> str:
    """Best single LAN address; localhost only if the device truly has no IP."""
    ips = lan_ips()
    return ips[0] if ips else "127.0.0.1"


def advertise(device_id: str, name: str, role: str, port: int) -> Zeroconf:
    """Publish this device on the network. Returns the Zeroconf handle, which
    the caller must keep alive for the advertisement to persist."""
    ip = primary_ip()
    info = ServiceInfo(
        SERVICE_TYPE,
        f"{device_id}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(ip)],
        port=port,
        properties={"device_id": device_id, "name": name, "role": role},
        server=f"eximaro-{device_id[:8]}.local.",
    )
    zc = Zeroconf()
    zc.register_service(info)
    return zc


class _Collector:
    def __init__(self) -> None:
        self.found: dict = {}

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if not info:
            return
        props = {
            (k.decode() if isinstance(k, bytes) else k):
            (v.decode() if isinstance(v, bytes) else v)
            for k, v in (info.properties or {}).items()
        }
        address = socket.inet_ntoa(info.addresses[0]) if info.addresses else ""
        device_id = props.get("device_id", name)
        self.found[device_id] = {
            "device_id": props.get("device_id", ""),
            "name": props.get("name", ""),
            "role": props.get("role", ""),
            "address": address,
            "port": info.port,
        }

    def update_service(self, *args) -> None:
        pass

    def remove_service(self, *args) -> None:
        pass


def browse(timeout: float = 3.0) -> list:
    """Look for devices on the network for a few seconds; return what answered."""
    zc = Zeroconf()
    collector = _Collector()
    ServiceBrowser(zc, SERVICE_TYPE, collector)
    time.sleep(timeout)
    zc.close()
    return list(collector.found.values())
