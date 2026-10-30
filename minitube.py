#!/usr/bin/env python3
"""MiniTube (Claude FM) — a tiny radio for YouTube live streams.

Lives in the Omarchy bar's tray: left click opens the popup, middle click
toggles power, the wheel sets the volume and right click has a menu. The
popup follows the current Omarchy theme. Audio is played by mpv (with
yt-dlp) over its JSON IPC socket.  Keyboard: space = power, arrows =
volume / tuning, m = mute, q / Esc = hide, Ctrl+Q = quit.

`minitube --tray` (login autostart) waits in the tray, silent, until used.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import gi

# Measured on this machine (Intel iGPU, 10 fps idle animation): the GL renderer
# uses about half the CPU of the default Vulkan one. An explicit GSK_RENDERER wins.
os.environ.setdefault("GSK_RENDERER", "gl")

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")

import cairo  # noqa: E402
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

APP_ID = "io.github.minitube"
APP_NAME = "MiniTube"
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "minitube")
STATIONS_FILE = os.path.join(CONFIG_DIR, "stations.json")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")

DEFAULT_STATIONS = [
    {
        "name": "CLAUDE FM",
        "url": "https://www.youtube.com/live/tRsQsTMvPNg",
    }
]

POPUP_W = 220
MASCOT_W, MASCOT_H = 40, 30

# Claude Code's mascot, transcribed from its "▐▛███▜▌ / ▝▜█████▛▘ / ▘▘ ▝▝"
# block glyphs into quadrant pixels: # body, o eye.
CLAWD = (
    "...############...",
    "...##o######o##...",
    ".################.",
    "...############...",
    "....#.#....#.#....",
)
CLAWD_ORANGE = "#D77757"


def C(value: str, alpha: float = 1.0):
    """'#rrggbb' -> cairo rgba."""
    value = value.lstrip("#")
    return (
        int(value[0:2], 16) / 255.0,
        int(value[2:4], 16) / 255.0,
        int(value[4:6], 16) / 255.0,
        alpha,
    )


def load_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return fallback


def save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


def clean_title(text: str) -> str:
    """Drop emoji / control characters that would render as tofu."""
    out = []
    for ch in text:
        code = ord(ch)
        if 0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF:
            continue
        if code == 0xFE0F or code == 0x200D:
            continue
        if ch in "\r\n\t":
            out.append(" ")
            continue
        if code < 0x20:
            continue
        out.append(ch)
    return " ".join("".join(out).split())


# --------------------------------------------------------------------------
# mpv backend
# --------------------------------------------------------------------------
class Mpv:
    """Runs mpv with an IPC socket and keeps a small state snapshot."""

    def __init__(self, on_state, volume=65):
        runtime = GLib.get_user_runtime_dir() or "/tmp"
        self.sock_path = os.path.join(runtime, "minitube-%d.sock" % os.getpid())
        self.on_state = on_state
        self.commands: queue.Queue = queue.Queue()
        self.desired = {"on": False, "url": None, "volume": volume}
        self.state = {"media-title": "", "pause": False, "idle-active": True, "volume": volume}
        self.proc = None
        self.alive = True
        self.debug = bool(os.environ.get("MINITUBE_DEBUG"))
        self.thread = threading.Thread(target=self._run, daemon=True)

    # -- public API (called from the GTK thread) ---------------------------
    def start(self):
        self.thread.start()

    def power(self, on: bool):
        self.commands.put(("power", on))

    def tune(self, url: str):
        self.commands.put(("tune", url))

    def volume(self, value: int):
        self.commands.put(("volume", value))

    def quit(self):
        self.alive = False
        self.commands.put(("quit", None))

    def shutdown(self, timeout=2.0):
        """Quit and wait: the thread is a daemon, so exiting first would orphan mpv."""
        self.quit()
        if self.thread.is_alive():
            self.thread.join(timeout)
        self._kill()

    # -- internals ---------------------------------------------------------
    def _spawn(self):
        args = [
            "mpv",
            "--no-video",
            "--idle=yes",
            "--no-terminal" if not self.debug else "--terminal",
            "--really-quiet" if not self.debug else "--msg-level=all=info",
            "--input-ipc-server=" + self.sock_path,
            "--audio-client-name=MiniTube",
            "--title=MiniTube",
            "--volume=%d" % int(self.desired["volume"]),
            "--volume-max=100",
            "--ytdl-format=bestaudio/best",
            "--no-resume-playback",
            "--cache=yes",
            "--demuxer-max-bytes=64MiB",
            "--demuxer-readahead-secs=30",
            "--network-timeout=20",
            "--force-window=no",
        ]
        try:
            self.proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=None if self.debug else subprocess.DEVNULL,
                stderr=None if self.debug else subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            self.proc = None

    def _connect(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.alive:
            if self.proc is None or self.proc.poll() is not None:
                return None
            if os.path.exists(self.sock_path):
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect(self.sock_path)
                    return sock
                except OSError:
                    pass
            time.sleep(0.1)
        return None

    def _run(self):
        while self.alive:
            try:
                os.unlink(self.sock_path)
            except OSError:
                pass
            self._spawn()
            sock = self._connect()
            if sock is None:
                self._kill()
                if self.alive:
                    time.sleep(1.0)
                continue
            try:
                self._loop(sock)
            except Exception:
                if self.debug:
                    import traceback

                    traceback.print_exc()
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
                self._kill()
            if self.alive:
                time.sleep(0.4)

    def _kill(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except OSError:
                    pass
        self.proc = None

    def _loop(self, sock):
        sock.setblocking(False)
        buf = b""
        rid = 0
        pending = {}
        last_poll = 0.0
        last_push = 0.0
        last_load = 0.0
        last_retry = 0.0

        def send(command):
            nonlocal rid
            rid += 1
            payload = {"command": command, "request_id": rid}
            try:
                sock.sendall((json.dumps(payload) + "\n").encode())
            except OSError:
                return None
            return rid

        def request(prop):
            nonlocal rid
            rid += 1
            payload = {"command": ["get_property", prop], "request_id": rid}
            try:
                sock.sendall((json.dumps(payload) + "\n").encode())
            except OSError:
                return
            pending[rid] = prop

        def load():
            nonlocal last_load, last_retry
            url = self.desired.get("url")
            if not url:
                return
            send(["loadfile", url, "replace"])
            send(["set_property", "pause", False])
            send(["set_property", "volume", int(self.desired["volume"])])
            last_load = time.monotonic()
            last_retry = last_load

        while self.alive:
            now = time.monotonic()

            while True:
                try:
                    kind, value = self.commands.get_nowait()
                except queue.Empty:
                    break
                if kind == "power":
                    self.desired["on"] = bool(value)
                    if value:
                        load()
                    else:
                        send(["stop"])
                        self.state["idle-active"] = True
                        self.state["pause"] = False
                        self.state["media-title"] = ""
                elif kind == "tune":
                    self.desired["url"] = value
                    if self.desired["on"]:
                        load()
                elif kind == "volume":
                    self.desired["volume"] = int(value)
                    send(["set_property", "volume", int(value)])
                    self.state["volume"] = int(value)
                elif kind == "quit":
                    self.alive = False
                    break

            if not self.alive:
                try:
                    send(["quit"])
                except Exception:
                    pass
                return

            if now - last_poll > 0.4:
                for prop in ("media-title", "pause", "idle-active", "volume"):
                    request(prop)
                last_poll = now

            try:
                ready, _, _ = select.select([sock], [], [], 0.05)
            except (OSError, ValueError):
                return

            if ready:
                try:
                    chunk = sock.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if "request_id" in msg:
                        prop = pending.pop(msg["request_id"], None)
                        if prop and msg.get("error") == "success" and "data" in msg:
                            self.state[prop] = msg["data"]

            # Recover if a live stream silently dropped.
            if (
                self.desired["on"]
                and self.desired.get("url")
                and self.state.get("idle-active")
                and now - last_load > 20
                and now - last_retry > 20
            ):
                load()

            if now - last_push > 0.3:
                last_push = now
                playing = (not self.state.get("idle-active")) and (not self.state.get("pause"))
                snapshot = {
                    "title": clean_title(str(self.state.get("media-title") or "")),
                    "playing": playing,
                    "idle": bool(self.state.get("idle-active")),
                    "paused": bool(self.state.get("pause")),
                }
                GLib.idle_add(self.on_state, snapshot)


# --------------------------------------------------------------------------
# tray icon: StatusNotifierItem + dbusmenu over Gio (Omarchy's bar is the host)
# --------------------------------------------------------------------------
_SNI = "org.kde.StatusNotifierItem"
_MENU = "com.canonical.dbusmenu"
_WATCHER = "org.kde.StatusNotifierWatcher"
_XML = """<node>
<interface name="org.kde.StatusNotifierItem">
  <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="u" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/>
  <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionMovieName" type="s" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <signal name="NewIcon"/><signal name="NewOverlayIcon"/><signal name="NewAttentionIcon"/>
  <signal name="NewToolTip"/><signal name="NewTitle"/>
  <signal name="NewStatus"><arg type="s"/></signal>
</interface>
<interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
    <arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
    <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/></method>
  <method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
  <method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/></method>
  <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
  <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
  <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
  <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
</interface>
</node>"""


class TrayItem:
    """Export a pixmap tray icon and a flat menu on the session bus."""

    def __init__(self, item_id, title, on_activate=None, on_secondary=None,
                 on_scroll=None, on_menu=None):
        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.on_activate = on_activate
        self.on_secondary = on_secondary
        self.on_scroll = on_scroll
        self.on_menu = on_menu
        self._connection = None
        self._exports = []
        self._owner_id = 0
        self._watch_id = 0
        self._owned = False
        self._registered = False
        self._generation = 0
        self._revision = 0
        self._nodes = {0: {"children-display": GLib.Variant("s", "submenu")}}
        self._children = []
        self._properties = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", item_id),
            "Title": GLib.Variant("s", title),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("u", 0),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", "/MenuBar"),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], title, "")),
        }
        for name in ("IconName", "OverlayIconName", "AttentionIconName",
                     "AttentionMovieName", "IconThemePath"):
            self._properties[name] = GLib.Variant("s", "")
        for name in ("IconPixmap", "OverlayIconPixmap", "AttentionIconPixmap"):
            self._properties[name] = GLib.Variant("a(iiay)", [])
        self._menu_properties = {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }

    def register(self):
        """Connect and register without blocking the main context."""
        if self._registered:
            return
        self._registered = True
        self._generation += 1
        generation = self._generation

        def connected(source, result):
            try:
                connection = Gio.bus_get_finish(result)
            except GLib.Error as error:
                self._registered = False
                print(f"Tray bus connection failed: {error}", file=sys.stderr)
                return
            if not self._registered or generation != self._generation:
                return
            self._connection = connection
            node = Gio.DBusNodeInfo.new_for_xml(_XML)
            for path, interface in (("/StatusNotifierItem", node.interfaces[0]),
                                    ("/MenuBar", node.interfaces[1])):
                self._exports.append(connection.register_object(
                    path, interface, self._method_call, self._get_property, None))
            self._owner_id = Gio.bus_own_name_on_connection(
                connection, self.bus_name, Gio.BusNameOwnerFlags.NONE,
                self._name_acquired, self._name_lost)
            self._watch_id = Gio.bus_watch_name(
                Gio.BusType.SESSION, _WATCHER, Gio.BusNameWatcherFlags.NONE,
                self._watcher_appeared, self._watcher_vanished)

        Gio.bus_get(Gio.BusType.SESSION, None, connected)

    def _name_acquired(self, connection, name):
        self._owned = True
        self._register_with_watcher()

    def _name_lost(self, connection, name):
        self._owned = False

    def _watcher_appeared(self, connection, name, owner):
        self._register_with_watcher()

    def _watcher_vanished(self, connection, name):
        pass

    def _register_with_watcher(self):
        if not self._owned or self._connection is None:
            return

        def completed(connection, result):
            try:
                connection.call_finish(result)
            except GLib.Error as error:
                if self._registered:
                    print(f"Tray watcher registration failed: {error}", file=sys.stderr)

        self._connection.call(
            _WATCHER, "/StatusNotifierWatcher", _WATCHER,
            "RegisterStatusNotifierItem", GLib.Variant("(s)", (self.bus_name,)),
            None, Gio.DBusCallFlags.NONE, 5000, None, completed)

    def _get_property(self, connection, sender, path, interface, name):
        properties = self._properties if interface == _SNI else self._menu_properties
        return properties.get(name)

    def _emit(self, path, interface, name, parameters=None):
        if self._connection is not None:
            self._connection.emit_signal(None, path, interface, name, parameters)

    def _changed(self, name, value, signal_name, parameters=None):
        self._properties[name] = value
        self._emit("/StatusNotifierItem", _SNI, signal_name, parameters)
        self._emit("/StatusNotifierItem", "org.freedesktop.DBus.Properties",
                   "PropertiesChanged", GLib.Variant("(sa{sv}as)", (_SNI, {name: value}, [])))

    def set_icon(self, pixmaps):
        data = []
        for width, height, pixels in pixmaps:
            if width <= 0 or height <= 0 or len(pixels) != width * height * 4:
                raise ValueError("Invalid ARGB pixmap dimensions or byte length")
            data.append((width, height, bytes(pixels)))
        self._changed("IconPixmap", GLib.Variant("a(iiay)", data), "NewIcon")

    def set_tooltip(self, title, description):
        self._changed("ToolTip", GLib.Variant("(sa(iiay)ss)",
                      ("", [], title, description)), "NewToolTip")

    def set_title(self, title):
        self._changed("Title", GLib.Variant("s", title), "NewTitle")

    def set_status(self, status):
        if status not in ("Active", "Passive", "NeedsAttention"):
            raise ValueError("Invalid StatusNotifierItem status")
        self._changed("Status", GLib.Variant("s", status), "NewStatus",
                      GLib.Variant("(s)", (status,)))

    def set_menu(self, items):
        nodes = {0: {"children-display": GLib.Variant("s", "submenu")}}
        children = []
        reserved = {item["id"] for item in items if item.get("type") != "separator"}
        separator_id = max(reserved | {0}) + 1
        for item in items:
            if item.get("type") == "separator":
                item_id = separator_id
                separator_id += 1
                properties = {"type": GLib.Variant("s", "separator")}
            else:
                item_id = item["id"]
                if not isinstance(item_id, int) or not 0 < item_id < 2**31 or item_id in nodes:
                    raise ValueError("Menu ids must be unique positive int32 values")
                toggle_type = item.get("toggle-type", "")
                toggle_state = item.get("toggle-state", 0)
                if toggle_type not in ("", "checkmark") or toggle_state not in (0, 1):
                    raise ValueError("Invalid menu toggle")
                properties = {
                    "label": GLib.Variant("s", item["label"]),
                    "enabled": GLib.Variant("b", item.get("enabled", True)),
                    "visible": GLib.Variant("b", item.get("visible", True)),
                    "toggle-type": GLib.Variant("s", toggle_type),
                    "toggle-state": GLib.Variant("i", toggle_state),
                }
            nodes[item_id] = properties
            children.append(item_id)
        self._nodes = nodes
        self._children = children
        self._revision = (self._revision + 1) % 2**32
        self._emit("/MenuBar", _MENU, "LayoutUpdated",
                   GLib.Variant("(ui)", (self._revision, 0)))

    def _node_properties(self, item_id, names):
        properties = self._nodes[item_id]
        return {key: value for key, value in properties.items() if not names or key in names}

    def _layout(self, item_id, depth, names):
        children = []
        if item_id == 0 and depth != 0:
            children = [GLib.Variant("(ia{sv}av)", self._layout(child, depth - 1, names))
                        for child in self._children]
        return item_id, self._node_properties(item_id, names), children

    def _event(self, item_id, event):
        if item_id not in self._nodes:
            return False
        properties = self._nodes[item_id]
        if (event == "clicked" and item_id != 0
                and properties.get("type", GLib.Variant("s", "")).unpack() != "separator"
                and properties.get("enabled", GLib.Variant("b", True)).unpack()
                and properties.get("visible", GLib.Variant("b", True)).unpack()
                and self.on_menu):
            self.on_menu(item_id)
        return True

    def _method_call(self, connection, sender, path, interface, method, parameters, invocation):
        args = parameters.unpack()
        try:
            if interface == _SNI:
                callback = {"Activate": self.on_activate, "SecondaryActivate": self.on_secondary,
                            "Scroll": self.on_scroll, "ContextMenu": None}[method]
                if callback:
                    callback(*args)
                result = None
            elif method == "GetLayout":
                result = GLib.Variant("(u(ia{sv}av))",
                                      (self._revision, self._layout(*args)))
            elif method == "GetGroupProperties":
                ids, names = args
                result = GLib.Variant("(a(ia{sv}))", ([(item_id, self._node_properties(item_id, names))
                    for item_id in (ids or list(self._nodes)) if item_id in self._nodes],))
            elif method == "GetProperty":
                result = GLib.Variant("(v)", (self._nodes[args[0]][args[1]],))
            elif method == "Event":
                if not self._event(args[0], args[1]):
                    raise KeyError(args[0])
                result = None
            elif method == "EventGroup":
                errors = [item_id for item_id, event, data, timestamp in args[0]
                          if not self._event(item_id, event)]
                result = GLib.Variant("(ai)", (errors,))
            elif method == "AboutToShow":
                if args[0] not in self._nodes:
                    raise KeyError(args[0])
                result = GLib.Variant("(b)", (False,))
            elif method == "AboutToShowGroup":
                result = GLib.Variant("(aiai)", ([], [i for i in args[0] if i not in self._nodes]))
            else:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
                return
            invocation.return_value(result)
        except KeyError as error:
            invocation.return_dbus_error("com.canonical.dbusmenu.Error.InvalidMenuItem", str(error))
        except Exception as error:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", str(error))

    def close(self):
        """Unexport the objects and release the bus name."""
        self._registered = False
        self._generation += 1
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
            self._watch_id = 0
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
            self._owner_id = 0
        if self._connection is not None:
            for registration in self._exports:
                self._connection.unregister_object(registration)
        self._exports.clear()
        self._owned = False
        self._connection = None


# --------------------------------------------------------------------------
# Omarchy theme: palette + font from the applied theme, live updates, tray icon
# --------------------------------------------------------------------------
_THEME_DEFAULTS = {
    "mode": "dark", "accent": "#7aa2f7", "background": "#1a1b26",
    "dark_background": "#13141c", "lighter_background": "#24283b",
    "foreground": "#a9b1d6", "dark_foreground": "#565f89",
    "bright_foreground": "#c0caf5", "muted": "#414868",
    "selection": "#292e42", "red": "#f7768e", "yellow": "#e0af68",
    "green": "#9ece6a", "orange": "#eb927b", "popup_bg": "#1a1b26",
    "popup_text": "#a9b1d6", "popup_border": "#7aa2f7",
    "control_fill_alpha": 0.04, "control_border_alpha": 0.4,
    "hover_fill_alpha": 0.08, "pressed_fill_alpha": 0.22,
    "selected_fill_alpha": 0.18, "font_family": "monospace",
    "font_size": 12, "name": "Tokyo Night",
}


def _theme_state_dir(state_dir):
    return Path(state_dir).expanduser() if state_dir is not None else (
        Path.home() / ".local/state/omarchy/current")


def _theme_toml(path):
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, ValueError):
        return {}


def _theme_resolve(value, shell, colors):
    """Follow palette and section.key references, rejecting cycles."""
    seen = set()
    while isinstance(value, str) and not value.startswith("#"):
        if value in seen:
            return None
        seen.add(value)
        if "." in value:
            section, key = value.split(".", 1)
            table = colors if section == "colors" else shell.get(section, {})
            value = table.get(key) if isinstance(table, dict) else None
        elif value in colors:
            value = colors[value]
        else:
            return None
    return value


def _theme_hex(value):
    if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return value.lower()
    if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{3}", value):
        return "#" + "".join(c * 2 for c in value[1:]).lower()
    return None


def load_theme(state_dir=None):
    """Read the applied theme, using Tokyo Night defaults for invalid keys."""
    result = dict(_THEME_DEFAULTS)
    try:
        state = _theme_state_dir(state_dir)
        colors = _theme_toml(state / "theme/colors.toml")
        shell = _theme_toml(state / "theme/shell.toml")
        for key, default in _THEME_DEFAULTS.items():
            if isinstance(default, str) and default.startswith("#"):
                color = _theme_hex(_theme_resolve(colors.get(key), shell, colors))
                if color:
                    result[key] = color
        if colors.get("mode") in ("dark", "light"):
            result["mode"] = colors["mode"]
        mapping = {
            "popup_bg": "popups.background", "popup_text": "popups.text",
            "popup_border": "popups.border",
            "control_fill_alpha": "controls.normal-fill-alpha",
            "control_border_alpha": "controls.normal-border-alpha",
            "hover_fill_alpha": "controls.hover-cursor-fill-alpha",
            "pressed_fill_alpha": "controls.pressed-fill-alpha",
            "selected_fill_alpha": "controls.selected-fill-alpha",
            "font_size": "font.base-size",
        }
        for key, reference in mapping.items():
            value = _theme_resolve(reference, shell, colors)
            if key.startswith("popup_"):
                # a non-hex token (e.g. a gradient border) falls back to this theme's own colours
                base = {"popup_bg": "background", "popup_text": "foreground",
                        "popup_border": "accent"}[key]
                result[key] = _theme_hex(value) or result[base]
            elif (isinstance(value, (int, float)) and not isinstance(value, bool)
                  and math.isfinite(value)):
                if key == "font_size" and value > 0:
                    result[key] = value
                elif key != "font_size" and 0 <= value <= 1:
                    result[key] = value
        try:
            result["name"] = (state / "theme.name").read_text().strip() or result["name"]
        except (OSError, UnicodeError):
            pass
    except Exception:
        # The public loader must remain safe even during a partially applied theme.
        pass
    try:
        font = subprocess.run(
            ["fc-match", "monospace", "-f", "%{family}"], capture_output=True,
            text=True, timeout=2, check=True).stdout.split(",", 1)[0].strip()
        if font:
            result["font_family"] = font
    except (OSError, subprocess.SubprocessError, UnicodeError):
        pass
    return result


class _ThemeWatch:
    """Keep the stable parent monitored while theme/ is replaced."""

    def __init__(self, callback, state_dir):
        self.state = _theme_state_dir(state_dir)
        self.callback = callback
        self.pending = 0
        self.cancelled = False
        self.last = load_theme(self.state)
        self.theme_monitor = None
        self.monitor = Gio.File.new_for_path(str(self.state)).monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES, None)
        self.monitor.connect("changed", self._changed)
        self._monitor_theme()

    def _monitor_theme(self):
        if self.theme_monitor:
            self.theme_monitor.cancel()
        self.theme_monitor = None
        try:
            self.theme_monitor = Gio.File.new_for_path(
                str(self.state / "theme")).monitor_directory(
                    Gio.FileMonitorFlags.WATCH_MOVES, None)
            self.theme_monitor.connect("changed", self._changed)
        except GLib.Error:
            pass

    def _changed(self, monitor, file, other_file, event):
        if self.cancelled:
            return
        if monitor is self.monitor and not any(
                item and item.get_basename() in ("theme", "theme.name")
                for item in (file, other_file)):
            return
        if self.pending:
            GLib.source_remove(self.pending)
        self.pending = GLib.timeout_add(300, self._notify)

    def _notify(self):
        self.pending = 0
        if not self.cancelled:
            self._monitor_theme()
            theme = load_theme(self.state)
            if theme != self.last:
                self.last = theme
                self.callback(theme)
        return GLib.SOURCE_REMOVE

    def cancel(self):
        """Stop monitoring and discard any queued notification."""
        self.cancelled = True
        if self.pending:
            GLib.source_remove(self.pending)
            self.pending = 0
        self.monitor.cancel()
        if self.theme_monitor:
            self.theme_monitor.cancel()


def watch_theme(callback, state_dir=None):
    """Notify on the GLib main context after a debounced theme change."""
    return _ThemeWatch(callback, state_dir)


def _theme_radio_surface(size, color, alpha=1.0):
    """Draw a small outlined radio with transparent dial and grille cutouts."""
    color = _theme_hex(color) or _THEME_DEFAULTS["accent"]
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    cr.scale(size / 22, size / 22)
    cr.set_source_rgb(*(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)))
    cr.set_line_width(1.5)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.move_to(5, 5)
    cr.line_to(14, 2)
    cr.stroke()
    # Rounded outline leaves every internal detail in the same single ink.
    cr.new_sub_path()
    for x, y, start in ((18, 8, -math.pi / 2), (18, 17, 0),
                        (4, 17, math.pi / 2), (4, 8, math.pi)):
        cr.arc(x, y, 2, start, start + math.pi / 2)
    cr.close_path()
    cr.stroke()
    cr.rectangle(5, 9, 12, 3)
    cr.stroke()
    cr.move_to(12, 9)
    cr.line_to(12, 12)
    cr.stroke()
    for x in (5.5, 8.5, 11.5):
        for y in (15, 17):
            cr.arc(x, y, 0.65, 0, math.tau)
            cr.fill()
    cr.arc(16.5, 16, 1.7, 0, math.tau)
    cr.fill()
    if alpha < 1:
        faded = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        context = cairo.Context(faded)
        context.set_source_surface(surface, 0, 0)
        context.paint_with_alpha(max(0.0, alpha))
        surface = faded
    surface.flush()
    return surface


def tray_icon_pixmaps(color, alpha=1.0):
    """Return straight-alpha, network-order ARGB pixmaps for the bar."""
    pixmaps = []
    for size in (16, 22, 32, 48):
        surface = _theme_radio_surface(size, color, alpha)
        source = memoryview(surface.get_data()).cast("B")
        stride = surface.get_stride()
        pixels = bytearray()
        for y in range(size):
            for x in range(size):
                offset = y * stride + x * 4
                value = int.from_bytes(source[offset:offset + 4], sys.byteorder)
                opacity = value >> 24
                channels = [(value >> shift) & 255 for shift in (16, 8, 0)]
                pixels.append(opacity)
                pixels.extend(min(255, (channel * 255 + opacity // 2) // opacity)
                              if opacity else 0 for channel in channels)
        pixmaps.append((size, size, bytes(pixels)))
    return pixmaps


def tray_host_available():
    """True when a StatusNotifierWatcher (the bar's tray) is on the session bus."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", GLib.Variant("(s)", (_WATCHER,)), GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE, 1000, None)
        return reply.unpack()[0]
    except GLib.Error:
        return False


# --------------------------------------------------------------------------
# the popup
# --------------------------------------------------------------------------
class MiniTube(Gtk.ApplicationWindow):
    def __init__(self, app, autoplay=True):
        super().__init__(application=app)
        self.set_title(APP_NAME)
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(POPUP_W, -1)
        self.add_css_class("minitube")

        self.config = load_json(STATIONS_FILE, None)
        if not self.config or not self.config.get("stations"):
            self.config = {"stations": [dict(s) for s in DEFAULT_STATIONS]}
            save_json(STATIONS_FILE, self.config)
        self.stations = [s for s in self.config["stations"] if s.get("url")]
        if not self.stations:
            self.stations = [dict(s) for s in DEFAULT_STATIONS]

        state = load_json(STATE_FILE, {})
        self.volume = max(0, min(100, int(state.get("volume", 65))))
        self.station_index = max(0, min(len(self.stations) - 1, int(state.get("station", 0))))
        self.muted = False

        self.power_on = False
        self.mode = "off"  # off | tuning | live | error
        self.title = ""
        self.tune_started = 0.0
        self.last_retry = 0.0
        self.now = time.monotonic()
        self._anim_id = None
        self._anim_interval = 0
        self._syncing = False

        self.theme = load_theme()
        self.css = Gtk.CssProvider()
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, self.css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        self._theme_watch = watch_theme(self._on_theme)

        self.mpv = Mpv(self._on_mpv_state, volume=self.volume)

        self._tray_state = {}
        self._tray_scroll_acc = 0
        self.tray = TrayItem("minitube", "Claude FM",
                             on_activate=lambda *_: self.toggle_popup(),
                             on_secondary=lambda *_: self._toggle_power(),
                             on_scroll=self._tray_scroll, on_menu=self._tray_menu)
        self._apply_theme()
        self.tray.register()

        self.set_child(self._build())

        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL | Gtk.EventControllerScrollFlags.DISCRETE
        )
        scroll.connect("scroll", self._scroll)
        self.add_controller(scroll)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)

        self.connect("close-request", self._on_close)

        self._sync()
        self.mpv.start()
        if autoplay:
            GLib.timeout_add(600, self._autoplay)

    # -- layout ------------------------------------------------------------
    def _button(self, icon, tip, handler, *classes):
        button = Gtk.Button(icon_name=icon)
        button.set_tooltip_text(tip)
        # keys belong to the window: a focused button would eat Space
        button.set_focusable(False)
        button.add_css_class("ctl")
        for name in classes:
            button.add_css_class(name)
        button.connect("clicked", lambda *_: handler())
        return button

    def _build(self):
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        frame.add_css_class("frame")

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.add_css_class("header")
        self.led = Gtk.DrawingArea()
        self.led.set_size_request(7, 7)
        self.led.set_valign(Gtk.Align.CENTER)
        self.led.set_draw_func(self._draw_led)
        header.append(self.led)
        name = Gtk.Label(label="Claude FM")
        name.add_css_class("app-name")
        header.append(name)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        header.append(spacer)
        self.state_label = Gtk.Label(label="OFF")
        self.state_label.add_css_class("state")
        self.state_label.set_valign(Gtk.Align.CENTER)
        header.append(self.state_label)
        self.close_btn = self._button("window-close-symbolic", "Hide (Esc)", self.hide_popup, "flat")
        header.append(self.close_btn)
        handle = Gtk.WindowHandle()
        handle.set_child(header)
        frame.append(handle)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        body.add_css_class("body")
        self.mascot = Gtk.DrawingArea()
        self.mascot.set_size_request(MASCOT_W, MASCOT_H)
        self.mascot.set_valign(Gtk.Align.CENTER)
        self.mascot.set_draw_func(self._draw_mascot)
        body.append(self.mascot)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text.set_valign(Gtk.Align.CENTER)
        text.set_hexpand(True)
        self.station_label = Gtk.Label(xalign=0.0)
        self.station_label.add_css_class("station")
        self.station_label.set_ellipsize(Pango.EllipsizeMode.END)
        text.append(self.station_label)
        self.title_label = Gtk.Label(xalign=0.0)
        self.title_label.add_css_class("track")
        self.title_label.set_wrap(True)
        self.title_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.title_label.set_lines(2)
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_label.set_max_width_chars(1)  # let the box, not the text, decide the width
        text.append(self.title_label)
        body.append(text)
        frame.append(body)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        controls.add_css_class("row")
        self.power_btn = self._button("system-shutdown-symbolic", "Power (Space)", self._toggle_power)
        self.prev_btn = self._button("media-skip-backward-symbolic", "Previous station (←)",
                                     lambda: self._next_station(-1))
        self.next_btn = self._button("media-skip-forward-symbolic", "Next station (→)",
                                     lambda: self._next_station(1))
        for button in (self.power_btn, self.prev_btn, self.next_btn):
            controls.append(button)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        controls.append(spacer)
        self.mute_btn = self._button("audio-volume-high-symbolic", "Mute (M)", self._toggle_mute)
        controls.append(self.mute_btn)
        frame.append(controls)

        volume = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        volume.add_css_class("row")
        volume.add_css_class("last")
        self.vol_down_btn = self._button("list-remove-symbolic", "Volume down (↓)",
                                         lambda: self._set_volume(self.volume - 5))
        self.vol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.vol_scale.set_draw_value(False)
        self.vol_scale.set_hexpand(True)
        self.vol_scale.set_focusable(False)
        self.vol_scale.connect("value-changed", self._on_scale)
        self.vol_up_btn = self._button("list-add-symbolic", "Volume up (↑)",
                                       lambda: self._set_volume(self.volume + 5))
        self.vol_label = Gtk.Label(label="")
        self.vol_label.add_css_class("vol")
        self.vol_label.set_width_chars(4)
        self.vol_label.set_xalign(1.0)
        for widget in (self.vol_down_btn, self.vol_scale, self.vol_up_btn, self.vol_label):
            volume.append(widget)
        frame.append(volume)
        return frame

    # -- theme -------------------------------------------------------------
    def _on_theme(self, theme):
        self.theme = theme
        self._apply_theme()

    def _apply_theme(self):
        t = self.theme
        css = CSS_TEMPLATE
        for key, value in t.items():
            css = css.replace("@" + key + "@", str(value))
        self.css.load_from_data(css.encode("utf-8"))
        self._icons = {True: tray_icon_pixmaps(t["accent"]),
                       False: tray_icon_pixmaps(t["foreground"], 0.45)}
        self._tray_state.pop("on", None)  # force the icon out again
        self._update_tray()
        for area in ("led", "mascot"):
            if hasattr(self, area):
                getattr(self, area).queue_draw()

    # -- app plumbing ------------------------------------------------------
    def _autoplay(self):
        self._power_on()
        return False

    def _save_state(self):
        save_json(STATE_FILE, {"volume": self.volume, "station": self.station_index})

    def _on_close(self, *_args):
        # closing the popup only hides it; the radio keeps playing in the tray
        self.hide_popup()
        return True

    def quit_app(self):
        if self._theme_watch is not None:
            self._theme_watch.cancel()
            self._theme_watch = None
        self._save_state()
        self.tray.close()
        self.mpv.shutdown()
        self.get_application().quit()
        return False

    def _on_mpv_state(self, snap):
        if not self.power_on:
            return False
        title = snap.get("title") or ""
        if title:
            self.title = title
        if snap.get("playing"):
            if self.mode != "live":
                self.mode = "live"
        elif snap.get("idle"):
            if self.mode == "live":
                self.mode = "tuning"
                self.tune_started = time.monotonic()
        self._sync()
        return False

    # -- popup + tray ------------------------------------------------------
    def show_popup(self):
        self.present()
        self._sync()

    def hide_popup(self):
        self.set_visible(False)
        self._sync()

    def toggle_popup(self):
        if self.get_visible():
            self.hide_popup()
        else:
            self.show_popup()

    def _tray_scroll(self, delta, orientation):
        if orientation != "vertical":
            return
        # touchpads send many small deltas; one notch of a wheel is 120
        self._tray_scroll_acc += delta
        while abs(self._tray_scroll_acc) >= 120:
            step = 1 if self._tray_scroll_acc > 0 else -1
            self._tray_scroll_acc -= step * 120
            self._set_volume(self.volume + 5 * step)

    def _tray_menu(self, item):
        if item == 1:
            self.toggle_popup()
        elif item == 2:
            self._toggle_power()
        elif item == 3:
            self._toggle_mute()
        elif item == 4:
            GLib.idle_add(self.quit_app)  # let the D-Bus call return first

    def _status_text(self):
        if not self.power_on:
            return "Power off"
        if self.mode == "error":
            return "No signal"
        if self.mode == "tuning":
            return "Tuning…"
        return self.title or "Live"

    def _update_tray(self):
        """Push icon, tooltip and menu to the tray, only when they changed."""
        on = self.power_on
        name = str(self.stations[self.station_index].get("name") or "UNTITLED")
        status = self._status_text()
        if on:
            status += " · muted" if self.muted else " · vol %d%%" % self.volume
        menu = [
            {"id": 1, "label": "Hide radio" if self.get_visible() else "Show radio"},
            {"id": 2, "label": "Power", "toggle-type": "checkmark", "toggle-state": int(on)},
            {"id": 3, "label": "Mute", "toggle-type": "checkmark",
             "toggle-state": int(self.muted), "enabled": on},
            {"type": "separator"},
            {"id": 4, "label": "Quit"},
        ]
        state = self._tray_state
        if state.get("on") != on:
            self.tray.set_icon(self._icons[on])
        if state.get("tip") != (name, status):
            self.tray.set_tooltip(name, status)
        if state.get("menu") != menu:
            self.tray.set_menu(menu)
        self._tray_state = {"on": on, "tip": (name, status), "menu": menu}

    def _sync(self):
        """Make labels, buttons, the tray and the animation timer match the state."""
        on = self.power_on
        if not hasattr(self, "state_label"):
            self._update_tray()
            return
        label = {"live": "LIVE", "tuning": "TUNING", "error": "NO SIGNAL"}.get(self.mode, "OFF")
        if not on:
            label = "OFF"
        self.state_label.set_label(label)
        for name in ("live", "tuning", "error", "off"):
            if name == (self.mode if on else "off"):
                self.state_label.add_css_class(name)
            else:
                self.state_label.remove_css_class(name)
        self.station_label.set_label(str(self.stations[self.station_index].get("name") or "UNTITLED"))
        self.title_label.set_label("Press power to listen" if not on else self._status_text())
        for widget, active in ((self.power_btn, on), (self.mute_btn, self.muted and on)):
            if active:
                widget.add_css_class("on")
            else:
                widget.remove_css_class("on")
        self.mute_btn.set_icon_name("audio-volume-muted-symbolic" if self.muted
                                    else "audio-volume-high-symbolic")
        multi = len(self.stations) > 1
        self.prev_btn.set_sensitive(multi)
        self.next_btn.set_sensitive(multi)
        self._syncing = True
        self.vol_scale.set_value(self.volume)
        self._syncing = False
        self.vol_label.set_label("mute" if self.muted else "%d%%" % self.volume)
        self.led.queue_draw()
        self.mascot.queue_draw()
        self._update_tray()
        self._schedule()

    # -- power / tuning / volume ------------------------------------------
    def _power_on(self):
        self.power_on = True
        self.mode = "tuning"
        self.tune_started = time.monotonic()
        self.title = ""
        self.mpv.volume(0 if self.muted else self.volume)
        self.mpv.power(True)
        self.mpv.tune(self.stations[self.station_index]["url"])
        self._sync()

    def _power_off(self):
        self.power_on = False
        self.mode = "off"
        self.title = ""
        self.mpv.power(False)
        self._sync()

    def _toggle_power(self):
        if self.power_on:
            self._power_off()
        else:
            self._power_on()

    def _next_station(self, step=1):
        index = (self.station_index + step) % len(self.stations)
        if index == self.station_index:
            return
        self.station_index = index
        self.title = ""
        self._save_state()
        if self.power_on:
            self.mode = "tuning"
            self.tune_started = time.monotonic()
            self.mpv.tune(self.stations[index]["url"])
        self._sync()

    def _set_volume(self, value):
        value = max(0, min(100, int(value)))
        if value == self.volume and not self.muted:
            return
        self.volume = value
        self.muted = False
        self.mpv.volume(self.volume)
        self._save_state()
        self._sync()

    def _on_scale(self, scale):
        if not self._syncing:
            self._set_volume(int(round(scale.get_value())))

    def _toggle_mute(self):
        self.muted = not self.muted
        self.mpv.volume(0 if self.muted else self.volume)
        self._sync()

    # -- input -------------------------------------------------------------
    def _scroll(self, _controller, _dx, dy):
        if dy == 0:
            return False
        self._set_volume(self.volume + (5 if dy < 0 else -5))
        return True

    def _key(self, _controller, keyval, _keycode, state):
        name = Gdk.keyval_name(keyval) or ""
        if name in ("q", "Q") and state & Gdk.ModifierType.CONTROL_MASK:
            self.quit_app()
            return True
        if name in ("space", "Return", "KP_Enter"):
            self._toggle_power()
            return True
        if name in ("Up", "KP_Add", "plus", "equal"):
            self._set_volume(self.volume + 5)
            return True
        if name in ("Down", "KP_Subtract", "minus"):
            self._set_volume(self.volume - 5)
            return True
        if name == "Right":
            self._next_station(1)
            return True
        if name == "Left":
            self._next_station(-1)
            return True
        if name in ("m", "M"):
            self._toggle_mute()
            return True
        if name in ("q", "Q", "Escape"):
            self.hide_popup()
            return True
        return False

    # -- animation ---------------------------------------------------------
    def _frame_interval(self):
        """ms between ticks: the mascot moves only while it can be seen, 0 = stop."""
        if not self.power_on:
            return 0
        if not self.get_visible():
            return 1000  # nothing to draw; keep the tuning timeout / retry clock going
        return 100

    def _schedule(self):
        want = self._frame_interval()
        if want == self._anim_interval and (self._anim_id is not None or not want):
            return
        if self._anim_id is not None:
            GLib.source_remove(self._anim_id)
            self._anim_id = None
        self._anim_interval = want
        if want:
            self._anim_id = GLib.timeout_add(want, self._tick)

    def _tick(self):
        now = time.monotonic()
        self.now = now
        mode = self.mode
        if self.mode == "tuning" and now - self.tune_started > 30:
            self.mode = "error"
            self.last_retry = now
        if self.mode == "error" and now - self.last_retry > 12:
            self.last_retry = now
            self.mode = "tuning"
            self.tune_started = now
            self.mpv.tune(self.stations[self.station_index]["url"])
        if self.mode != mode:
            self._sync()
        self.mascot.queue_draw()
        if self._frame_interval() == self._anim_interval:
            return GLib.SOURCE_CONTINUE
        self._anim_id = None
        self._anim_interval = 0
        self._schedule()
        return GLib.SOURCE_REMOVE

    # -- drawing -----------------------------------------------------------
    def _draw_led(self, area, cr, w, h):
        color = self.theme["green"] if self.mode == "live" else self.theme["accent"]
        if not self.power_on:
            color = self.theme["dark_foreground"]
        cr.set_source_rgba(*C(color))
        cr.arc(w / 2, h / 2, min(w, h) / 2, 0, 2 * math.pi)
        cr.fill()

    def _draw_mascot(self, area, cr, w, h):
        """Claude Code's mascot, bouncing to the radio."""
        now = time.monotonic()
        on = self.power_on
        pw, ph = 2.0, 3.6  # terminal block pixels are twice as tall as wide
        cols, rows = len(CLAWD[0]), len(CLAWD)
        floor = h - 2
        mx = (w - cols * pw) / 2
        bob = 0.0
        legs_lifted = False
        if on and self.mode == "live" and not self.muted:
            bob = -1.4 if (now * 2.2) % 1.0 < 0.5 else 0.0
        elif on and self.mode == "tuning":
            mx += round(math.sin(now * 2.0) * 3)
            legs_lifted = int(now * 6) % 2 == 1
        closed = not on or (now % 3.7) < 0.14
        body = C(CLAWD_ORANGE, 1.0 if on else 0.45)
        eye = C(self.theme["popup_bg"])
        top = floor - rows * ph
        for row, line in enumerate(CLAWD):
            is_legs = row == rows - 1
            ry = top + row * ph + (0.0 if is_legs else bob)
            for col, ch in enumerate(line):
                if ch == "." or (is_legs and legs_lifted and col in (4, 13)):
                    continue
                if ch == "o" and not closed:
                    continue
                cr.rectangle(mx + col * pw, ry, pw + 0.05, ph + 0.05)
            cr.set_source_rgba(*body)
            cr.fill()
            if row == 1:
                for col, ch in enumerate(line):
                    if ch != "o":
                        continue
                    if closed:
                        cr.rectangle(mx + col * pw - 0.3, ry + ph * 0.55, pw + 0.6, 0.7)
                    else:
                        cr.rectangle(mx + col * pw, ry, pw + 0.05, ph + 0.05)
                cr.set_source_rgba(*eye)
                cr.fill()

        right = mx + cols * pw
        if not on:
            cr.set_source_rgba(*C(self.theme["dark_foreground"]))
            for i, (dx, dy, s) in enumerate(((-3, -2, 3.0), (1, -7, 4.0))):
                x0, y0 = right + dx, top + dy
                cr.set_line_width(0.9)
                cr.move_to(x0, y0)
                cr.line_to(x0 + s, y0)
                cr.line_to(x0, y0 + s)
                cr.line_to(x0 + s, y0 + s)
                cr.stroke()
        elif self.mode == "live" and not self.muted:
            cr.set_source_rgba(*C(self.theme["accent"]))
            phase = (now * 0.6) % 1.0
            nx = right - 1 + math.sin(phase * 6.0)
            ny = floor - 6 - phase * (h - 12)
            cr.save()
            cr.translate(nx, ny)
            cr.scale(1.3, 1.0)
            cr.arc(0, 0, 1.2, 0, 2 * math.pi)
            cr.restore()
            cr.fill()
            cr.set_line_width(0.6)
            cr.move_to(nx + 1.4, ny)
            cr.line_to(nx + 1.4, ny - 4.5)
            cr.line_to(nx + 3.2, ny - 3.4)
            cr.stroke()


# --------------------------------------------------------------------------
# Omarchy look: every colour and the font come from the current theme
# (@key@ placeholders are filled from load_theme()).
CSS_TEMPLATE = """
window.minitube {
  background: @popup_bg@;
  color: @popup_text@;
  font-family: "@font_family@", monospace;
  font-size: 11px;
}
.frame { border: 2px solid @popup_border@; }
.header { padding: 5px 4px 3px 10px; }
.app-name { font-weight: 700; color: @bright_foreground@; }
.state {
  font-size: 9px;
  font-weight: 700;
  padding: 0 5px;
  color: @dark_foreground@;
  border: 1px solid alpha(@dark_foreground@, 0.6);
}
.state.live { color: @green@; border-color: alpha(@green@, 0.6); }
.state.tuning { color: @yellow@; border-color: alpha(@yellow@, 0.6); }
.state.error { color: @red@; border-color: alpha(@red@, 0.6); }
.body { padding: 4px 10px 6px 10px; }
.station { font-weight: 700; color: @bright_foreground@; }
.track { font-size: 10px; color: @dark_foreground@; }
.row { padding: 0 10px 4px 10px; }
.row.last { padding-bottom: 9px; }
button.ctl {
  min-width: 24px;
  min-height: 20px;
  padding: 0 3px;
  border-radius: 0;
  box-shadow: none;
  color: @foreground@;
  background: alpha(@foreground@, @control_fill_alpha@);
  border: 1px solid alpha(@foreground@, @control_border_alpha@);
}
button.ctl:hover { background: alpha(@foreground@, @hover_fill_alpha@); }
button.ctl:active { background: alpha(@foreground@, @pressed_fill_alpha@); }
button.ctl:disabled { color: alpha(@foreground@, 0.3); border-color: alpha(@foreground@, 0.15); }
button.ctl.on { color: @accent@; border-color: @accent@; }
button.ctl.flat { border-color: transparent; background: transparent; min-width: 18px; min-height: 18px; }
button.ctl.flat:hover { background: alpha(@foreground@, @hover_fill_alpha@); }
scale { padding: 0 2px; }
scale trough { min-height: 4px; border-radius: 0; background: @lighter_background@; border: none; }
scale highlight { border-radius: 0; background: @accent@; }
scale slider {
  min-width: 8px;
  min-height: 12px;
  margin: -4px 0;
  border-radius: 0;
  border: none;
  box-shadow: none;
  background: @bright_foreground@;
}
.vol { font-size: 10px; color: @dark_foreground@; }
"""


class MiniTubeApp(Gtk.Application):
    def __init__(self, tray_only=False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window = None
        self.tray_only = tray_only

    def do_startup(self):
        Gtk.Application.do_startup(self)
        # logout, kill or Ctrl+C: stop mpv too instead of orphaning it
        try:
            gi.require_version("GLibUnix", "2.0")
            from gi.repository import GLibUnix
            signal_add = GLibUnix.signal_add
        except (ImportError, ValueError):
            signal_add = GLib.unix_signal_add
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_signal)

    def _on_signal(self):
        if self.window is not None:
            self.window.quit_app()
        else:
            self.quit()
        return GLib.SOURCE_REMOVE

    def do_activate(self):
        if self.window is None:
            self.window = MiniTube(self, autoplay=not self.tray_only)
            # --tray never pops up: at login the bar may come up after us, and
            # the tray item registers itself as soon as it does
            if self.tray_only or tray_host_available():
                return  # start in the tray
        self.window.show_popup()


def main():
    if shutil.which("mpv") is None:
        sys.stderr.write("MiniTube needs mpv: pacman -S mpv\n")
        return 1
    if shutil.which("yt-dlp") is None and shutil.which("youtube-dl") is None:
        sys.stderr.write("MiniTube needs yt-dlp: pacman -S yt-dlp\n")
        return 1
    GLib.set_prgname("minitube")
    GLib.set_application_name(APP_NAME)
    tray_only = "--tray" in sys.argv[1:]
    app = MiniTubeApp(tray_only=tray_only)
    return app.run([arg for arg in sys.argv if arg != "--tray"])


if __name__ == "__main__":
    sys.exit(main())
