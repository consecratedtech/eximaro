# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The root helper runs nmcli (so it's exercised on hardware), but its terse-output
parser is pure and easy to get wrong, so we test it directly. The helper has no
.py extension, so we load it from its path."""

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

HELPER = Path(__file__).resolve().parent.parent / "helpers" / "eximaro-wifi"


def _load():
    # The helper has no .py extension, so point a source loader at it explicitly.
    loader = SourceFileLoader("eximaro_wifi_helper", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_split_terse_plain():
    assert _load()._split_terse("Home:80:WPA2") == ["Home", "80", "WPA2"]


def test_split_terse_escaped_colon_in_ssid():
    # nmcli renders an SSID that literally contains ':' as 'My\:Net'.
    assert _load()._split_terse(r"My\:Net:64:WPA2") == ["My:Net", "64", "WPA2"]


def test_split_terse_open_network_has_blank_security():
    assert _load()._split_terse("Cafe:50:") == ["Cafe", "50", ""]


def test_split_terse_trailing_backslash_does_not_crash():
    assert _load()._split_terse("Odd\\") == ["Odd\\"]


# --- what the helper writes back must never carry the Wi-Fi password ----------------

import json  # noqa: E402
import subprocess  # noqa: E402

SECRET = "correct-horse-42"


def _run(tmp_path, monkeypatch, nmcli, action="connect", ssid="Home Net", nft=None):
    """Run the helper's main() for one request against a fake nmcli; return the status.
    With `nft`, the setup network's redirect goes to it instead of being skipped."""
    mod = _load()
    monkeypatch.setattr(mod, "REQUEST", tmp_path / "wifi.request")
    monkeypatch.setattr(mod, "STATUS", tmp_path / "wifi-status.json")
    monkeypatch.setattr(mod, "_nmcli", nmcli)
    if nft:
        monkeypatch.setattr(mod, "_nft", nft)
    else:
        monkeypatch.setattr(mod, "_captive_on", lambda: None)
        monkeypatch.setattr(mod, "_captive_off", lambda: None)
    monkeypatch.setattr(mod, "_set_country", lambda: None)
    clock = {"t": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(mod.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    mod.REQUEST.write_text(json.dumps({"action": action, "ssid": ssid, "password": SECRET}))
    mod.main()
    return mod.STATUS.read_text()


def _ok(*args, **kw):
    return subprocess.CompletedProcess(args, 0, "", "")


class FakeNM:
    """Just enough NetworkManager for a join. Saved networks are {uuid: profile}, a
    profile being {name, ssid, connected, settings}. `visible_after` is how many plain
    scans until the network shows up (None: never); a `hidden` network shows up only once
    a scan asks for it by name. `join` decides how the join goes: "ok", "fail" (nmcli says
    no), "timeout" (nmcli gives up by itself) or "hang" (nmcli is still going when the
    helper's own limit runs out). Like nmcli, a join saves its network under the name it
    is given before it knows whether it worked."""

    def __init__(self, saved=None, join="ok", visible_after=0, hidden=False, hotspot_up=True,
                 list_fails=0, dev="wlan0", ssid="Home Net"):
        self.saved, self.n = {}, 0
        for name, ssid in (saved or {}).items():
            self.add(name, ssid, True)
        self.add("Hotspot", "Eximaro-Setup-0000", True, uuid="u-hotspot")
        self.join, self.visible_after, self.hidden, self.hotspot_up = join, visible_after, hidden, hotspot_up
        self.list_fails, self.dev, self.ssid = list_fails, dev, ssid
        self.scans = self.named_scans = 0
        self.calls = []

    def add(self, name, ssid, connected, uuid=None, **settings):
        self.n += 1
        uuid = uuid or f"{self.n:08x}-0000-4000-8000-000000000000"
        self.saved[uuid] = {"name": name, "ssid": ssid, "connected": connected, "settings": settings}
        return uuid

    def names(self):
        return sorted(p["name"] for p in self.saved.values())

    def by_name(self, name):
        return [p for p in self.saved.values() if p["name"] == name]

    def __call__(self, *args, timeout=30):
        self.calls.append((args, timeout))
        a = list(args)
        if a == ["-t", "-f", "DEVICE,TYPE", "device", "status"]:
            return subprocess.CompletedProcess(args, 0, f"{self.dev}:wifi\neth0:ethernet\n", "")
        if a[:3] == ["-t", "-f", "NAME,UUID,TYPE,TIMESTAMP"]:
            if self.list_fails:
                self.list_fails -= 1
                return subprocess.CompletedProcess(args, 8, "", "Error: NetworkManager is not running.")
            lines = [f"{p['name']}:{u}:802-11-wireless:{1793600000 if p['connected'] else 0}"
                     for u, p in self.saved.items()]
            lines.append("Wired connection 1:u-eth:802-3-ethernet:1793600000")
            return subprocess.CompletedProcess(args, 0, "\n".join(lines) + "\n", "")
        if a[:2] == ["-g", "802-11-wireless.ssid"]:
            p = self.saved.get(a[-1])
            return subprocess.CompletedProcess(args, 0 if p else 10, (p["ssid"] + "\n") if p else "", "")
        if a[0] == "-g":                       # the settings a re-joined network carries over
            p = self.saved.get(a[-1])
            keys = a[1].split(",")
            return subprocess.CompletedProcess(args, 0, "\n".join(p["settings"].get(k, "") for k in keys) + "\n", "")
        if a[:3] == ["device", "wifi", "rescan"]:
            if "ssid" in a:
                self.named_scans += 1
            else:
                self.scans += 1
            return _ok(*args)
        if a[:3] == ["-t", "-f", "SSID"]:
            seen = (self.named_scans > 0) if self.hidden else (
                self.visible_after is not None and self.scans > self.visible_after)
            return subprocess.CompletedProcess(args, 0, (f"{self.ssid}\nNeighbours\n" if seen else "Neighbours\n"), "")
        if "connect" in a:
            ssid = a[a.index("connect") + 1]
            uuid = self.add(a[a.index("name") + 1], ssid, False)
            if self.join == "ok":
                self.saved[uuid]["connected"] = True
                return subprocess.CompletedProcess(
                    args, 0, f"Device '{self.dev}' successfully activated with '{uuid}'.\n", "")
            if self.join == "fail":
                return subprocess.CompletedProcess(args, 4, "", "Error: Connection activation failed")
            if self.join == "timeout":
                return subprocess.CompletedProcess(args, 3, "", "Error: Timeout 75 sec expired.")
            raise subprocess.TimeoutExpired(cmd=["nmcli", *args], timeout=timeout)
        if a[:3] == ["connection", "delete", "uuid"]:
            self.saved.pop(a[3], None)
            return _ok(*args)
        if a == ["connection", "delete", "Hotspot"]:
            for u in [u for u, p in self.saved.items() if p["name"] == "Hotspot"]:
                del self.saved[u]
            return _ok(*args)
        if a[:3] == ["connection", "modify", "uuid"]:
            p = self.saved[a[3]]
            pairs = a[4:]
            for k, v in zip(pairs[::2], pairs[1::2]):
                if k == "connection.id":
                    p["name"] = v
                else:
                    p["settings"][k] = v
            return _ok(*args)
        if a == ["connection", "up", "Hotspot"]:
            return _ok(*args) if self.hotspot_up else subprocess.CompletedProcess(args, 10, "", "Error")
        return _ok(*args)

    def ran(self, *args):
        return any(list(c[0][:len(args)]) == list(args) for c in self.calls)

    def joined_with(self):
        return [list(c[0]) for c in self.calls if "connect" in c[0]]


def test_a_join_that_times_out_never_records_the_password(tmp_path, monkeypatch):
    # subprocess puts the whole command line - password included - in this error.
    nm = FakeNM(join="hang")
    st = json.loads(_run(tmp_path, monkeypatch, nm))
    assert SECRET not in json.dumps(st)
    assert st["error"] == "could not join Home Net"
    assert st["detail"] == "the network took too long to answer"


def test_a_join_lets_nmcli_itself_give_up_first(tmp_path, monkeypatch):
    nm = FakeNM()
    _run(tmp_path, monkeypatch, nm)
    (args, timeout), = [c for c in nm.calls if "connect" in c[0]]
    assert list(args[:2]) == ["--wait", "75"]
    assert timeout > 75


def test_a_join_from_the_setup_network_waits_for_its_network_to_show_up(tmp_path, monkeypatch):
    # Right after the setup network goes down the radio's list of nearby networks is
    # empty, and nmcli joins only a network on it: it said "No network with SSID found"
    # at once, every time, so a screen could never be put on Wi-Fi this way.
    nm = FakeNM(visible_after=2)
    _run(tmp_path, monkeypatch, nm)
    (args,) = nm.joined_with()
    assert "hidden" not in args                  # it was seen, so asked for normally
    assert nm.scans >= 3
    assert "Home Net" in nm.names() and "eximaro-joining" not in nm.names()


def test_a_hidden_network_is_found_by_name_and_saved_as_hidden(tmp_path, monkeypatch):
    # It doesn't announce its name: plain scans never show it, a scan for it by name does.
    nm = FakeNM(hidden=True)
    _run(tmp_path, monkeypatch, nm)
    (args,) = nm.joined_with()
    assert args[-2:] == ["hidden", "yes"]
    assert nm.named_scans >= 1
    assert nm.names() == ["Home Net"]


def test_a_network_that_never_shows_up_is_still_asked_for_by_name(tmp_path, monkeypatch):
    nm = FakeNM(visible_after=None)
    _run(tmp_path, monkeypatch, nm)
    (args,) = nm.joined_with()
    assert args[-2:] == ["hidden", "yes"]


def test_a_join_that_works_takes_the_network_s_name_and_drops_the_setup_one(tmp_path, monkeypatch):
    nm = FakeNM()
    st = json.loads(_run(tmp_path, monkeypatch, nm))
    assert nm.names() == ["Home Net"]
    (p,) = nm.by_name("Home Net")
    assert p["connected"]
    assert p["settings"]["connection.autoconnect"] == "yes"
    assert p["settings"]["connection.autoconnect-retries"] == "0"
    assert not nm.ran("connection", "up", "Hotspot")
    assert SECRET not in json.dumps(st)


def test_a_network_named_like_the_setup_one_is_kept(tmp_path, monkeypatch):
    # Joined, renamed after itself - and then removed with the setup network, which goes
    # by the same name: stranded, saying nothing.
    nm = FakeNM(ssid="Hotspot")
    _run(tmp_path, monkeypatch, nm, ssid="Hotspot")
    assert nm.names() == ["Hotspot (Wi-Fi)"]
    (p,) = nm.by_name("Hotspot (Wi-Fi)")
    assert p["ssid"] == "Hotspot" and p["connected"]


def test_a_join_that_works_replaces_an_older_copy_of_the_same_network(tmp_path, monkeypatch):
    # Its new password is the one that just worked; the old copy may hold a stale one.
    nm = FakeNM(saved={"netplan-wlan0-Home Net": "Home Net", "Office": "Office"})
    _run(tmp_path, monkeypatch, nm)
    assert nm.names() == ["Home Net", "Office"]


def test_a_rejoined_network_keeps_its_own_settings(tmp_path, monkeypatch):
    # A fixed address and a hidden name, set up by hand, survive entering the network
    # again (with its new password, say).
    nm = FakeNM()
    nm.add("Home Net", "Home Net", True, **{"802-11-wireless.hidden": "yes", "ipv4.method": "manual",
                                            "ipv4.addresses": "192.0.2.50/24", "ipv4.gateway": "192.0.2.1",
                                            "ipv4.dns": "192.0.2.1", "ipv6.method": "ignore"})
    _run(tmp_path, monkeypatch, nm)
    (p,) = nm.by_name("Home Net")
    assert p["connected"]
    for key, value in (("802-11-wireless.hidden", "yes"), ("ipv4.method", "manual"),
                       ("ipv4.addresses", "192.0.2.50/24"), ("ipv4.gateway", "192.0.2.1"),
                       ("ipv4.dns", "192.0.2.1"), ("ipv6.method", "ignore")):
        assert p["settings"].get(key) == value, key


def test_a_join_settles_only_the_network_it_brought_up(tmp_path, monkeypatch):
    # An earlier failed try left its network behind and couldn't be tidied (nmcli was
    # busy). The next join, which works, must settle its own network - never the leftover
    # in its place, which would hold the wrong network and a wrong password.
    nm = FakeNM(list_fails=1)
    left = nm.add("eximaro-joining", "Neighbours", True)
    _run(tmp_path, monkeypatch, nm)
    assert left not in nm.saved
    (p,) = nm.by_name("Home Net")
    assert p["ssid"] == "Home Net" and p["connected"]


def test_the_wifi_device_is_found_not_assumed(tmp_path, monkeypatch):
    # Not every machine calls it wlan0.
    joined = FakeNM(dev="wlp2s0")
    _run(tmp_path, monkeypatch, joined)
    (args,) = joined.joined_with()
    assert args[args.index("ifname") + 1] == "wlp2s0"
    rules = []
    hosted = FakeNM(dev="wlp2s0")
    _run(tmp_path, monkeypatch, hosted, action="hotspot",
         nft=lambda *a, input_text=None: rules.append(input_text or ""))
    assert hosted.ran("device", "wifi", "hotspot", "ifname", "wlp2s0")
    assert 'iifname "wlp2s0"' in "".join(rules)
    assert all("wlan0" not in c[0] for c in joined.calls + hosted.calls)


def test_a_slow_join_brings_the_setup_network_back_and_saves_nothing(tmp_path, monkeypatch):
    # The old helper died on its own 45 s limit here: no setup network again, and the
    # half-made network left saved made the screen look set up - stranded.
    for join in ("timeout", "hang", "fail"):
        nm = FakeNM(join=join)
        st = json.loads(_run(tmp_path, monkeypatch, nm))
        assert nm.names() == ["Hotspot"], join
        assert nm.ran("connection", "up", "Hotspot"), join
        assert st["error"] == "could not join Home Net" and st["ap_active"] is True, join


def test_a_mistyped_password_for_a_saved_network_leaves_it_alone(tmp_path, monkeypatch):
    # The screen is on Office; someone re-enters Office with a typo. The join used to
    # write the typo into the saved network itself - cut off, and nothing to fall back on.
    import copy
    nm = FakeNM(saved={"Office": "Office"}, join="fail")
    before = copy.deepcopy(nm.saved)
    _run(tmp_path, monkeypatch, nm)
    assert nm.saved == before
    (args,) = nm.joined_with()
    assert args[args.index("name") + 1] == "eximaro-joining"


def test_nothing_is_deleted_when_the_saved_networks_cant_be_listed(tmp_path, monkeypatch):
    nm = FakeNM(saved={"Office": "Office", "Church": "Church"}, join="fail", list_fails=99)
    _run(tmp_path, monkeypatch, nm)
    assert not nm.ran("connection", "delete", "uuid")
    # What the failed join saved stays for now (the next run tidies it); nothing else goes.
    assert sorted(nm.names()) == ["Church", "Hotspot", "Office", "eximaro-joining"]


def test_a_join_cut_off_part_way_is_tidied_up_next_time(tmp_path, monkeypatch):
    # The power went mid-join: what it saved never connected. Kept, it would make the
    # screen look set up for good. Any later run of the helper removes it.
    nm = FakeNM(saved={"Office": "Office"})
    nm.add("eximaro-joining", "Home Net", False)
    _run(tmp_path, monkeypatch, nm, action="status")
    assert sorted(nm.names()) == ["Hotspot", "Office"]


def test_a_join_that_worked_but_was_cut_off_is_settled_next_time(tmp_path, monkeypatch):
    nm = FakeNM()
    nm.add("eximaro-joining", "Home Net", True)
    _run(tmp_path, monkeypatch, nm, action="status")
    assert "Home Net" in nm.names() and "eximaro-joining" not in nm.names()


def test_a_join_under_way_does_not_make_the_screen_look_set_up(tmp_path, monkeypatch):
    nm = FakeNM()
    nm.add("eximaro-joining", "Home Net", False)
    mod = _load()
    monkeypatch.setattr(mod, "_nmcli", nm)
    assert mod._has_saved_wifi() is False


def test_an_unexpected_failure_records_only_its_kind(tmp_path, monkeypatch):
    def nmcli(*args, timeout=30):
        if "connect" in args:
            raise RuntimeError(f"broke while using {SECRET}")
        return _ok(*args)
    text = _run(tmp_path, monkeypatch, nmcli)
    assert SECRET not in text
    st = json.loads(text)
    assert "RuntimeError" in st["detail"] and st["ap_active"] is True   # the setup network came back


def test_a_failed_join_blanks_the_password_in_nmcli_s_own_words(tmp_path, monkeypatch):
    def nmcli(*args, timeout=30):
        if "connect" in args:
            return subprocess.CompletedProcess(args, 4, "", f"Error: secrets {SECRET} were rejected")
        return _ok(*args)
    st = json.loads(_run(tmp_path, monkeypatch, nmcli))
    assert SECRET not in json.dumps(st)
    assert st["error"] == "could not join Home Net" and "***" in st["detail"]
    assert st["ap_active"] is True           # the setup network comes back for a retry


def test_no_setup_network_to_bring_back_is_said_plainly(tmp_path, monkeypatch):
    # A screen that was already set up has no setup network saved to bring back.
    nm = FakeNM(saved={"Office": "Office"}, join="fail", hotspot_up=False)
    st = json.loads(_run(tmp_path, monkeypatch, nm))
    assert st["ap_active"] is False


def test_a_failed_hotspot_blanks_its_password(tmp_path, monkeypatch):
    def nmcli(*args, timeout=30):
        if "hotspot" in args:
            return subprocess.CompletedProcess(args, 1, "", f"Error: bad key {SECRET}")
        return _ok(*args)
    text = _run(tmp_path, monkeypatch, nmcli, action="hotspot")
    assert SECRET not in text


def test_scrub_blanks_before_trimming():
    # A password that straddles the 200-character cut must not survive in part.
    text = "x" * 195 + SECRET
    assert "correct" not in _load()._scrub(text, SECRET)
