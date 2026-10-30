#!/usr/bin/env python3
"""In-process GTK integration test; run only with the MiniTube instance slot free."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.youtube.com/live/tRsQsTMvPNg"
SOCKET = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / f"minitube-{os.getpid()}.sock"
finished = threading.Event()
owned_mpvs = set()
failures = []


def discover_mpvs():
    """Only claim direct children of this test, never another test's player."""
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            status = (proc / 'status').read_text()
            ppid = next(line.split()[1] for line in status.splitlines() if line.startswith('PPid:'))
            cmd = (proc / 'cmdline').read_bytes().split(b'\0')
            if int(ppid) == os.getpid() and b'--audio-client-name=MiniTube' in cmd:
                owned_mpvs.add(int(proc.name))
        except (OSError, StopIteration, ValueError):
            pass


def alive(pid):
    try:
        # Zombies are exited, not running players.
        return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0] != 'Z'
    except OSError:
        return False


def cleanup():
    discover_mpvs()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in owned_mpvs:
            if alive(pid):
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
        until = time.monotonic() + 0.6
        while any(alive(p) for p in owned_mpvs) and time.monotonic() < until:
            time.sleep(0.02)


def watchdog():
    if not finished.wait(148):
        print('FAIL hard timeout: test exceeded 148 seconds; cleaning up before 150s', flush=True)
        cleanup()
        os._exit(1)


def prop(name):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.35)
        sock.connect(str(SOCKET))
        sock.sendall((json.dumps({'command': ['get_property', name], 'request_id': 91}) + '\n').encode())
        with sock.makefile('r') as reader:
            for line in reader:
                reply = json.loads(line)
                if reply.get('request_id') == 91:
                    if reply.get('error') != 'success':
                        raise RuntimeError(f'mpv {name}: {reply}')
                    return reply.get('data')
    raise RuntimeError(f'No mpv response for {name}')


def require(ok, detail):
    if not ok:
        raise AssertionError(detail)


def wait_for(predicate, description, seconds=5):
    yield predicate, description, seconds


def screenshot(path):
    clients = json.loads(subprocess.check_output(['hyprctl', 'clients', '-j'], timeout=3))
    client = next((c for c in clients if c.get('pid') == os.getpid() and c.get('mapped', True)), None)
    require(client is not None, f'No mapped Hyprland client for PID {os.getpid()}')
    x, y = client['at']
    width, height = client['size']
    subprocess.run(['grim', '-g', f'{x},{y} {width}x{height}', path], check=True, timeout=5)
    require(Path(path).stat().st_size > 0, f'Empty screenshot {path}')


def exercise(app, config, Gdk):
    yield from wait_for(lambda: app.window is not None, 'app.window exists')
    w = app.window
    for attr in ('power_btn', 'prev_btn', 'next_btn', 'mute_btn', 'vol_down_btn', 'vol_up_btn',
                 'close_btn', 'vol_scale', 'station_label', 'title_label', 'state_label',
                 'power_on', 'muted', 'volume', 'station_index', 'mode', 'title',
                 'show_popup', 'hide_popup', 'toggle_popup', 'quit_app', '_key'):
        require(hasattr(w, attr), f'Missing contract attribute window.{attr}')
    w.show_popup()
    yield from wait_for(w.get_mapped, 'show_popup maps window')
    print('PASS 1 show_popup maps popup', flush=True)

    def live():
        return w.mode == 'live' and w.state_label.get_text() == 'LIVE' and bool(w.title_label.get_text().strip()) and prop('idle-active') is False

    yield from wait_for(live, 'mode live, LIVE label, nonempty title, mpv playing', 30)
    discover_mpvs()
    print('PASS 2 live playback and labels', flush=True)
    w.vol_down_btn.emit('clicked')
    w.vol_down_btn.emit('clicked')
    yield from wait_for(lambda: w.volume == 30 and prop('volume') == 30 and w.vol_scale.get_value() == 30,
                        'two volume-down clicks: state/IPC/scale 30')
    w.vol_up_btn.emit('clicked')
    yield from wait_for(lambda: w.volume == 35 and prop('volume') == 35, 'volume-up: 35')
    w.vol_scale.set_value(70)
    yield from wait_for(lambda: w.volume == 70 and prop('volume') == 70 and
                        json.loads((config / 'state.json').read_text())['volume'] == 70,
                        'scale and saved state/IPC: 70')
    print('PASS 3 volume buttons, scale, IPC and saved state', flush=True)
    w.mute_btn.emit('clicked')
    yield from wait_for(lambda: w.muted and prop('volume') == 0, 'mute: flag true and IPC volume 0')
    w.mute_btn.emit('clicked')
    yield from wait_for(lambda: not w.muted and prop('volume') == 70, 'unmute: IPC volume 70')
    print('PASS 4 mute/unmute restores volume', flush=True)
    w.next_btn.emit('clicked')
    yield from wait_for(lambda: w.station_index == 1 and w.station_label.get_text() == 'TEST FM' and
                        str(prop('path')).endswith('?test=2'), 'next: TEST FM/index 1/path ?test=2')
    w.prev_btn.emit('clicked')
    yield from wait_for(lambda: w.station_index == 0 and w.station_label.get_text() == 'CLAUDE FM',
                        'previous: CLAUDE FM/index 0')
    print('PASS 5 station next/previous and mpv path', flush=True)
    w.power_btn.emit('clicked')
    yield from wait_for(lambda: not w.power_on and w.state_label.get_text() == 'OFF' and prop('idle-active') is True,
                        'power off: flag/OFF/IPC idle')
    w.power_btn.emit('clicked')
    yield from wait_for(live, 'power on returns to live', 30)
    print('PASS 6 power off/on', flush=True)
    w._key(None, Gdk.KEY_space, 0, Gdk.ModifierType(0))
    yield from wait_for(lambda: not w.power_on and prop('idle-active') is True, 'space powers off')
    w._key(None, Gdk.KEY_space, 0, Gdk.ModifierType(0))
    yield from wait_for(live, 'space powers on', 30)
    w._key(None, Gdk.KEY_Up, 0, Gdk.ModifierType(0))
    yield from wait_for(lambda: w.volume == 75 and prop('volume') == 75, 'Up raises volume to 75')
    w._key(None, Gdk.KEY_m, 0, Gdk.ModifierType(0))
    yield from wait_for(lambda: w.muted and prop('volume') == 0, 'm mutes')
    w._key(None, Gdk.KEY_m, 0, Gdk.ModifierType(0))
    yield from wait_for(lambda: not w.muted and prop('volume') == 75, 'm unmutes')
    print('PASS 7 space/Up/m key handler', flush=True)
    w.close_btn.emit('clicked')
    yield from wait_for(lambda: not w.get_mapped(), 'close button hides popup')
    require(app.get_is_registered(), 'app stopped after hiding')
    print('PASS 8 close hides popup without quitting', flush=True)
    w.show_popup()
    yield from wait_for(w.get_mapped, 'popup visible for screenshot')
    yield from wait_for(lambda: True, 'allow live frame to render', 1)
    screenshot('/tmp/minitube-ui-live.png')
    w.power_btn.emit('clicked')
    yield from wait_for(lambda: w.state_label.get_text() == 'OFF' and prop('idle-active') is True,
                        'OFF screenshot state')
    yield from wait_for(lambda: True, 'allow OFF frame to render', 1)
    screenshot('/tmp/minitube-ui-off.png')
    print('PASS 9 screenshots /tmp/minitube-ui-live.png /tmp/minitube-ui-off.png', flush=True)
    discover_mpvs()
    w.quit_app()


def main():
    threading.Thread(target=watchdog, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='minitube-ui-') as tmp:
        os.environ['XDG_CONFIG_HOME'] = tmp
        config = Path(tmp) / 'minitube'
        config.mkdir()
        (config / 'stations.json').write_text(json.dumps({'stations': [
            {'name': 'CLAUDE FM', 'url': URL}, {'name': 'TEST FM', 'url': URL + '?test=2'}]}))
        (config / 'state.json').write_text(json.dumps({'volume': 40, 'station': 0}))
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Gdk', '4.0')
        from gi.repository import GLib, Gdk
        spec = importlib.util.spec_from_file_location('minitube', ROOT / 'minitube.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules['minitube'] = module
        spec.loader.exec_module(module)
        app = module.MiniTubeApp()
        steps = exercise(app, config, Gdk)
        pending = None
        completed = False

        def tick():
            nonlocal pending, completed
            try:
                discover_mpvs()
                if pending is not None:
                    predicate, description, deadline = pending
                    try:
                        ready = predicate()
                        observed = 'predicate returned false'
                    except (OSError, RuntimeError, ValueError) as exc:
                        ready, observed = False, str(exc)
                    if not ready:
                        require(time.monotonic() < deadline, f'{description}; got {observed}')
                        return True
                predicate, description, seconds = next(steps)
                pending = predicate, description, time.monotonic() + seconds
                return True
            except StopIteration:
                completed = True
                return False
            except Exception as exc:
                failures.append(str(exc))
                print(f'FAIL UI check: {exc}', flush=True)
                traceback.print_exc()
                app.quit()
                return False

        GLib.timeout_add(150, tick)
        code = app.run([str(ROOT / 'minitube.py')])
        # quit_app can stop the loop before the generator resumes to StopIteration.
        if not failures:
            try:
                next(steps)
            except StopIteration:
                completed = True
        until = time.monotonic() + 4
        while any(alive(pid) for pid in owned_mpvs) and time.monotonic() < until:
            time.sleep(0.05)
        if code == 0 and completed and not any(alive(pid) for pid in owned_mpvs):
            print('PASS 10 quit_app returned and no owned mpv remains', flush=True)
        else:
            failures.append(f'quit: run status={code}, completed={completed}, remaining mpv={[p for p in owned_mpvs if alive(p)]}')
            print('FAIL 10 ' + failures[-1], flush=True)
    return 1 if failures else 0


if __name__ == '__main__':
    try:
        result = main()
    except BaseException:
        traceback.print_exc()
        print('FAIL test setup or execution exception', flush=True)
        result = 1
    finally:
        cleanup()
        finished.set()
    sys.exit(result)
