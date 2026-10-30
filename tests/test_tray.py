#!/usr/bin/env python3
"""Black-box checks for MiniTube's session tray and process lifecycle."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib


APP = Path(__file__).resolve().parents[1] / "minitube.py"
SNI = "org.kde.StatusNotifierItem"
MENU = "com.canonical.dbusmenu"
WATCHER = "org.kde.StatusNotifierWatcher"
APP_ID = "io.github.minitube"


def wait_for(predicate, timeout=5, description="condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (OSError, ValueError, KeyError, GLib.Error) as error:
            last = str(error)
        time.sleep(0.1)
    raise AssertionError(f"expected {description} within {timeout}s; got {last or 'false'}")


class Harness:
    def __init__(self, config):
        self.config = Path(config)
        directory = self.config / "minitube"
        directory.mkdir()
        url = "https://www.youtube.com/live/tRsQsTMvPNg"
        (directory / "stations.json").write_text(json.dumps({"stations": [
            {"name": "CLAUDE FM", "url": url},
            {"name": "TEST FM", "url": url + "?test=2"},
        ]}))
        (directory / "state.json").write_text(json.dumps({"volume": 40, "station": 0}))
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.process = None
        self.log = None
        self.mpv_pids = set()
        self.name = None
        self.ipc_path = None
        self.failures = []

    def call(self, destination, path, interface, method, signature=None, args=()):
        parameters = GLib.Variant(signature, args) if signature else None
        reply = self.bus.call_sync(destination, path, interface, method, parameters,
                                   None, Gio.DBusCallFlags.NONE, 3000, None)
        return reply.unpack()

    def prop(self, name):
        return self.call(self.name, "/StatusNotifierItem", "org.freedesktop.DBus.Properties",
                         "Get", "(ss)", (SNI, name))[0]

    def registered(self):
        return self.call(WATCHER, "/StatusNotifierWatcher", "org.freedesktop.DBus.Properties",
                         "Get", "(ss)", (WATCHER, "RegisteredStatusNotifierItems"))[0]

    def owner(self, name):
        return self.call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                         "org.freedesktop.DBus", "GetNameOwner", "(s)", (name,))[0]

    def launch(self):
        try:
            owner = self.owner(APP_ID)
        except GLib.Error as error:
            if not error.matches(Gio.dbus_error_quark(), Gio.DBusError.NAME_HAS_NO_OWNER):
                raise
        else:
            raise RuntimeError(f"refusing to launch while existing MiniTube owns {APP_ID}: {owner}")
        env = dict(os.environ, XDG_CONFIG_HOME=str(self.config))
        self.log = tempfile.TemporaryFile(mode="w+t")
        self.process = subprocess.Popen([sys.executable, str(APP)], env=env,
                                        stdout=self.log, stderr=self.log)
        self.name = f"org.kde.StatusNotifierItem-{self.process.pid}-1"
        self.ipc_path = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / f"minitube-{self.process.pid}.sock"
        wait_for(lambda: self.prop("Id") == "minitube", description="owned tray item")
        self.unique_name = self.owner(self.name)
        wait_for(lambda: self.ipc_path.exists(), description="mpv IPC socket")
        self.find_mpv()

    def find_mpv(self):
        if self.ipc_path is None:
            return set()
        found = set()
        marker = f"--input-ipc-server={self.ipc_path}".encode()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                args = (entry / "cmdline").read_bytes().split(b"\0")
                if marker in args and args and Path(os.fsdecode(args[0])).name == "mpv":
                    found.add(int(entry.name))
            except (OSError, ProcessLookupError):
                continue
        self.mpv_pids.update(found)
        return found

    def ipc(self, property_name):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(self.ipc_path))
            client.sendall((json.dumps({"command": ["get_property", property_name],
                                        "request_id": 1}) + "\n").encode())
            with client.makefile("r") as stream:
                while True:
                    line = stream.readline()
                    if not line:
                        raise OSError("mpv closed IPC before replying")
                    reply = json.loads(line)
                    if reply.get("request_id") == 1:
                        if reply.get("error") != "success":
                            raise ValueError(f"mpv {property_name}: {reply}")
                        return reply.get("data")

    def visible(self):
        result = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True,
                                text=True, check=True, timeout=3)
        return any(client.get("class") == APP_ID and client.get("mapped", True)
                   for client in json.loads(result.stdout))

    def tray(self, method, signature="(ii)", args=(0, 0)):
        self.call(self.name, "/StatusNotifierItem", SNI, method, signature, args)

    def event(self, item_id):
        self.call(self.name, "/MenuBar", MENU, "Event", "(isvu)",
                  (item_id, "clicked", GLib.Variant("s", ""), 0))

    def saved_volume(self):
        return json.loads((self.config / "minitube/state.json").read_text())["volume"]

    def volume(self, expected):
        try:
            wait_for(lambda: abs(self.ipc("volume") - expected) < 0.01,
                     description=f"mpv volume {expected}")
        except AssertionError as error:
            raise AssertionError(f"expected mpv volume {expected}; got {self.ipc('volume')}; "
                                 f"saved volume {self.saved_volume()}") from error
        actual = self.ipc("volume")
        assert abs(actual - expected) < 0.01, f"expected mpv volume {expected}; got {actual}"

    def check(self, label, action):
        try:
            action()
        except Exception as error:
            self.failures.append((label, str(error)))
            print(f"FAIL {label}: {error}", flush=True)
        else:
            print(f"PASS {label}", flush=True)

    def assert_equal(self, actual, expected):
        assert actual == expected, f"expected {expected!r}; got {actual!r}"

    def verify_registration(self):
        entries = self.registered()
        assert any(entry in (self.name, self.name + "/StatusNotifierItem",
                             self.unique_name + "/StatusNotifierItem") for entry in entries), entries
        self.assert_equal(self.prop("Id"), "minitube")
        self.assert_equal(self.prop("Title"), "Claude FM")
        pixmaps = self.prop("IconPixmap")
        assert pixmaps and all(len(data) == width * height * 4 for width, height, data in pixmaps), "expected nonempty valid IconPixmap"

    def verify_popup(self):
        self.assert_equal(self.visible(), False)
        self.tray("Activate")
        wait_for(self.visible, description="popup mapped after Activate")
        self.tray("Activate")
        wait_for(lambda: not self.visible(), description="popup hidden after second Activate")

    def verify_power(self):
        wait_for(lambda: self.ipc("idle-active") is False, timeout=30,
                 description="initial playback idle-active=false")
        self.icon_on = self.prop("IconPixmap")
        self.tray("SecondaryActivate")
        wait_for(lambda: self.ipc("idle-active") is True, description="power-off idle-active=true")
        wait_for(lambda: self.prop("IconPixmap") != self.icon_on, description="power-off icon differs")
        self.tray("SecondaryActivate")
        wait_for(lambda: self.ipc("idle-active") is False, timeout=30,
                 description="resumed playback idle-active=false")

    def verify_scroll(self):
        self.volume(40)
        for delta, expected in ((120, 45), (-240, 35)):
            self.tray("Scroll", "(is)", (delta, "vertical"))
            self.volume(expected)
            wait_for(lambda: self.saved_volume() == expected,
                     description=f"state.json volume {expected}")

    def verify_menu(self):
        revision, root = self.call(self.name, "/MenuBar", MENU, "GetLayout", "(iias)", (0, -1, []))
        self.assert_equal(root[0], 0)
        self.assert_equal(root[1].get("children-display"), "submenu")
        children = {child[0]: child[1] for child in root[2]}
        for item_id, label in ((2, "Power"), (3, "Mute"), (4, "Quit")):
            self.assert_equal(children[item_id]["label"], label)
        assert children[1]["label"] in ("Show radio", "Hide radio"), children[1]
        assert len(children) == 5 and sum(p.get("type") == "separator" for p in children.values()) == 1, children
        for item_id in (2, 3):
            self.assert_equal(children[item_id].get("toggle-type"), "checkmark")
        self.assert_equal(children[3].get("enabled"), True)
        restore = self.ipc("volume")
        self.event(3)
        self.volume(0)
        wait_for(lambda: "muted" in self.prop("ToolTip")[3], description="muted tooltip")
        self.event(3)
        self.volume(restore)

    def verify_tooltip(self):
        tooltip = self.prop("ToolTip")
        self.assert_equal(tooltip[2], "CLAUDE FM")

    def verify_exit(self):
        self.process.wait(timeout=5)
        self.assert_equal(self.process.returncode, 0)
        wait_for(lambda: not self.find_mpv(), description="owned mpv exited")
        wait_for(lambda: not any(entry.startswith((self.name, self.unique_name + "/"))
                                  for entry in self.registered()), description="tray unregistered")
        saved = json.loads((self.config / "minitube/state.json").read_text())
        assert "volume" in saved and "station" in saved, saved

    def quit_menu(self):
        self.event(4)
        self.verify_exit()

    def quit_signal(self):
        self.launch()
        self.process.send_signal(signal.SIGTERM)
        self.verify_exit()

    def cleanup(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        remaining = self.find_mpv()
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if remaining:
            time.sleep(0.3)
        for pid in self.find_mpv():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.log:
            self.log.seek(0)
            output = self.log.read()
            if self.failures and output:
                print("APP LOG:\n" + output, flush=True)
            self.log.close()
            self.log = None


def main():
    with tempfile.TemporaryDirectory(prefix="minitube-tray-test-") as config:
        harness = Harness(config)
        try:
            harness.launch()
            harness.check("1 tray registration and properties", harness.verify_registration)
            harness.check("2 hidden startup and Activate toggle", harness.verify_popup)
            harness.check("3 SecondaryActivate power and icon", harness.verify_power)
            harness.check("4 Scroll volume and saved state", harness.verify_scroll)
            harness.check("5 menu layout and mute toggle", harness.verify_menu)
            harness.check("6 station tooltip title", harness.verify_tooltip)
            harness.check("7 menu Quit cleans process, mpv and tray", harness.quit_menu)
            harness.cleanup()
            if harness.process.poll() is None or harness.find_mpv():
                raise RuntimeError("first launch did not clean up")
            harness.check("8 SIGTERM cleans second launch", harness.quit_signal)
        except Exception as error:
            harness.failures.append(("setup/lifecycle", str(error)))
            print(f"FAIL setup/lifecycle: {error}", flush=True)
        finally:
            harness.cleanup()
        return int(bool(harness.failures))


if __name__ == "__main__":
    raise SystemExit(main())
