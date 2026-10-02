#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Bundled Kilix provider: public launcher, private X app, pixels and input."""
import hashlib
import importlib.util
import io
import json
import os
import shutil
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch as mock_patch
from PIL import Image
from Xlib import X, XK, display
from Xlib.ext import xtest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from kilix_apps import private_directory
from kilix_settings import BUTTONS, read_settings
from control import request, events as control_events

for options in (['--fps', '0'], ['--size', '0x10'], ['--refit-windows', '--no-refit-windows']):
    bad = subprocess.run([str(ROOT / 'kilix'), 'run', *options, '--', sys.executable],
                         capture_output=True, text=True, timeout=5)
    assert bad.returncode == 2, (options, bad.stderr)
bad = subprocess.run([str(ROOT / 'kilix'), 'run', '--', '/nonexistent-batty-provider-command'],
                     capture_output=True, text=True, timeout=5)
assert bad.returncode == 1 and 'unavailable' in bad.stderr, bad.stderr
for package in ('kilix-apps', 'kitty-frame-presenter'):
    base = ROOT / 'third_party' / package
    manifest = json.loads((base / 'upstream.json').read_text())
    for name, expected in manifest['files'].items():
        expected = manifest.get('patched_files', {}).get(name, expected)
        assert hashlib.sha256((base / name).read_bytes()).hexdigest() == expected, name
    if manifest.get('patches'):
        with tempfile.TemporaryDirectory(prefix='bt-vendor-') as directory:
            restored = Path(directory)
            for name in manifest['patched_files']:
                (restored / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(base / name, restored / name)
            for patch in reversed(manifest['patches']):
                path = ROOT / patch['path']
                assert hashlib.sha256(path.read_bytes()).hexdigest() == patch['sha256']
                subprocess.run(['git', 'apply', '--reverse', str(path)], cwd=restored, check=True)
            for name in manifest['patched_files']:
                assert hashlib.sha256((restored / name).read_bytes()).hexdigest() == manifest['files'][name]

for package in ('kilix-desktop', 'kilix-content', 'kilix-state', 'kilix-telemetry'):
    base = ROOT / 'third_party' / package
    manifest = json.loads((base / 'upstream.json').read_text())
    for name, upstream_hash in manifest['files'].items():
        expected = manifest.get('adapted_files', {}).get(name, upstream_hash)
        assert hashlib.sha256((base / name).read_bytes()).hexdigest() == expected, (package, name)
    assert all((base / name).is_file() for name in ('README.md', 'LICENSE'))
    if package == 'kilix-desktop':
        assert (base / 'src/kilix').is_symlink()
        assert (base / 'src/kilix').resolve() == ROOT / 'kilix'
        for dependency in ('kilix-content', 'kilix-state', 'kilix-telemetry',
                           'kitty-frame-presenter'):
            link = base / 'src/third_party' / dependency
            assert link.is_symlink() and link.resolve() == ROOT / 'third_party' / dependency

stream_path = ROOT / 'third_party/kilix-apps/config/stream.py'
assert stream_path.read_bytes() == (ROOT / 'third_party/kilix-desktop/src/config/stream.py').read_bytes(), \
    'Bundled desktop and application providers must share the hardened stream supervisor'
stream_spec = importlib.util.spec_from_file_location('batty_provider_stream', stream_path)
provider_stream = importlib.util.module_from_spec(stream_spec)
stream_spec.loader.exec_module(provider_stream)
with mock_patch.object(provider_stream.glob, 'glob', return_value=[
        '/dev/dri/renderD128', '/dev/dri/renderD129']), \
     mock_patch.object(provider_stream.subprocess, 'run', side_effect=[
        SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)]):
    provider_stream._VAAPI_DEVICE_CACHE = None
    assert provider_stream._vaapi_device() == '/dev/dri/renderD129'
with mock_patch.dict(os.environ, {'KILIX_HW': '1'}), \
     mock_patch.object(provider_stream.subprocess, 'run', return_value=SimpleNamespace(
         stdout='libx264 h264_vaapi')):
    provider_stream._H264_CACHE = None
    assert provider_stream._h264_encoder() == 'vaapi'
    assert provider_stream._video_encode_args(15, 2)[0] == [
        '-vaapi_device', '/dev/dri/renderD129']
with mock_patch.dict(os.environ, {'KILIX_HW': '1'}), \
     mock_patch.object(provider_stream.subprocess, 'run', return_value=SimpleNamespace(
         stdout='libx264 h264_vaapi')), \
     mock_patch.object(provider_stream, '_vaapi_device', return_value=None):
    provider_stream._H264_CACHE = None
    assert provider_stream._h264_encoder() == 'x264'
with tempfile.TemporaryDirectory(prefix='bt-provider-codecs-') as directory:
    with mock_patch.dict(os.environ, {'KILIX_SESSION_HOME': directory}):
        supervisor = provider_stream.StreamSupervisor('audio-codecs')
        try:
            with mock_patch.object(supervisor, '_spawn_enc',
                                   side_effect=lambda name, argv, piped: argv):
                rtsp = supervisor.start_rtsp_pub(70, 320, 240, 32001,
                                                 audio='private.monitor')
                ts = supervisor.start_ts(70, 320, 240, 32002,
                                         audio='private.monitor')
                hls = supervisor.start_hls(70, 320, 240, directory + '/hls',
                                           audio='private.monitor')
            assert rtsp[rtsp.index('-c:a') + 1] == 'libopus'
            assert rtsp[rtsp.index('-g') + 1] == '15'
            assert ts[ts.index('-c:a') + 1] == 'aac'
            assert hls[hls.index('-c:a') + 1] == 'aac'
        finally:
            supervisor.cleanup()
with tempfile.TemporaryDirectory(prefix='bt-webrtc-config-') as directory:
    with mock_patch.dict(os.environ, {'KILIX_SESSION_HOME': directory}):
        supervisor = provider_stream.StreamSupervisor('webrtc-auth')
        try:
            with mock_patch.object(supervisor, 'ensure_mediamtx', return_value='/bin/true'), \
                 mock_patch.object(supervisor, 'spawn', return_value=SimpleNamespace()), \
                 mock_patch.object(provider_stream, 'wait_port', return_value=True):
                supervisor.start_mediamtx(rtsp_port=32001, webrtc_port=32002,
                                          token='test-token')
            config = (Path(supervisor.runtime_dir) / 'mediamtx.yml').read_text()
            anonymous = config.split('- user: any\n', 1)[1].split('- user: kilix\n', 1)[0]
            viewer = config.split('- user: kilix\n', 1)[1].split('paths:\n', 1)[0]
            assert '- action: publish' in anonymous and '- action: read' not in anonymous
            assert 'pass: test-token' in viewer and '- action: read' in viewer
            with mock_patch.object(supervisor, 'ensure_mediamtx', return_value='/bin/true'), \
                 mock_patch.object(supervisor, 'spawn', return_value=SimpleNamespace()), \
                 mock_patch.object(provider_stream, 'wait_port', return_value=True):
                supervisor.start_mediamtx(rtsp_port=32003, webrtc_port=32004,
                                          token='test-token', lan=True,
                                          tls=('/private/cert.pem', '/private/key.pem', 'fingerprint'))
            config = (Path(supervisor.runtime_dir) / 'mediamtx.yml').read_text()
            assert 'webrtcAddress: :32004' in config
            assert 'webrtcEncryption: yes' in config
            assert 'webrtcServerKey: "/private/key.pem"' in config
            assert 'webrtcServerCert: "/private/cert.pem"' in config
            with mock_patch.object(supervisor, 'ensure_mediamtx',
                                   side_effect=AssertionError('binary must not be requested')):
                try:
                    supervisor.start_mediamtx(rtsp_port=32005, webrtc_port=32006,
                                              token='test-token', lan=True)
                except RuntimeError as error:
                    assert 'requires TLS' in str(error)
                else:
                    raise AssertionError('LAN WebRTC started without TLS')
        finally:
            supervisor.cleanup()
with tempfile.TemporaryDirectory(prefix='bt-mediamtx-pin-') as directory:
    with mock_patch.dict(os.environ, {'KILIX_STORAGE_HOME': directory,
                                      'KILIX_DATA_HOME': str(Path(directory) / 'data'),
                                      'KILIX_SESSION_HOME': str(Path(directory) / 'session')}):
        supervisor = provider_stream.StreamSupervisor('pin-check')
        try:
            binary = Path(directory) / 'data/mediamtx/mediamtx'
            binary.parent.mkdir(parents=True, mode=0o700)
            binary.write_bytes(b'not the pinned executable')
            binary.chmod(0o700)
            try:
                supervisor.ensure_mediamtx()
            except RuntimeError as error:
                assert 'differs from pinned release' in str(error)
            else:
                raise AssertionError('modified MediaMTX executable was accepted')
            binary.unlink()
            with mock_patch.object(provider_stream.urllib.request, 'urlopen',
                                   return_value=io.BytesIO(b'wrong release archive')):
                try:
                    supervisor.ensure_mediamtx()
                except RuntimeError as error:
                    assert 'archive differs from pinned release' in str(error)
                else:
                    raise AssertionError('modified MediaMTX archive was accepted')
            assert not binary.exists() and not list(binary.parent.glob('.mediamtx-*'))
        finally:
            supervisor.cleanup()

with tempfile.TemporaryDirectory(prefix='bt-provider-') as directory:
    root = Path(directory)
    unsafe = root / 'unsafe'
    unsafe.mkdir(mode=0o755)
    unsafe.chmod(0o755)
    try:
        private_directory(unsafe)
    except ValueError:
        pass
    else:
        raise AssertionError('Unsafe application storage was accepted')
    assert unsafe.stat().st_mode & 0o777 == 0o755
    storage = root / 'storage'
    settings_file = root / 'settings.conf'
    settings_file.write_text('KILIX_CHROME_TAB_BAR_EDGE=bottom\nKILIX_CHROME_START_MENU=on\n' +
                             ''.join('KILIX_CHROME_BUTTON_' + name + '=off\n' for name in BUTTONS))
    env = os.environ | {'BATTY_PROVIDER_TEST': directory, 'BATTY_KILIX_STORAGE_HOME': str(storage),
        'BATTY_KILIX_CONFIG': str(ROOT / 'tests/provider_config.bash'),
        'GPU_TERMINAL_SETTINGS_FILE': str(settings_file), 'BATTY_TAB_BAR_EDGE': '',
        'BATTY_CONTROL_DIR': str(root / 'registry'), 'XDG_RUNTIME_DIR': directory, 'XDG_STATE_HOME': str(root / 'state'),
        'KILIX_STORAGE_HOME': str(root / 'foreign-storage'), 'KILIX_SESSION': 'foreign-session',
        'SDL_VIDEO_X11_WMCLASS': 'inherited-x11-app', 'SDL_VIDEO_WAYLAND_WMCLASS': 'inherited-wayland-app'}
    env.pop('BATTY_CONTROL', None)
    literal = 'spaces and $(false); literal'
    applications = root / 'xdg/applications'
    applications.mkdir(parents=True)
    (applications / 'provider.desktop').write_text(
        '[Desktop Entry]\nType=Application\nName=Provider acceptance\nCategories=Graphics;\n'
        'Exec="' + sys.executable + '" "' + str(ROOT / 'tests/provider_app.py') + '" "' + literal + '"\n'
        'Path=' + directory + '\n')
    (root / 'catalog_terminal.py').write_text(
        'from pathlib import Path\nimport sys,time\n'
        'Path("catalog-launched").write_text(sys.argv[1])\ntime.sleep(20)\n')
    (applications / 'terminal.desktop').write_text(
        '[Desktop Entry]\nType=Application\nName=ZZ Terminal acceptance\nTerminal=true\n'
        'Exec="' + sys.executable + '" "' + str(root / 'catalog_terminal.py') + '" "' + literal + '"\n'
        'Path=' + directory + '\n')
    env.update(XDG_DATA_HOME=str(applications.parent), XDG_DATA_DIRS=str(root / 'empty-xdg'))
    process = subprocess.Popen([str(ROOT / 'kilix'), 'apps', 'open', 'provider.desktop'],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    started = time.monotonic()
    # This exercise includes provider startup, two native hosts, live settings
    # and framebuffer checks; its timings are observations, not a latency gate.
    deadline = started + 55
    capture_state = {}
    capture_history = []
    wait_history = []

    def wait_for(predicate):
        began = time.monotonic()
        line = sys._getframe(1).f_lineno
        attempts = 0
        while not predicate():
            attempts += 1
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, (
                'Provider integration deadline', events(), dimensions(), dimensions('second'), capture_state, capture_history,
                {'line': line, 'elapsed': round(time.monotonic()-began, 3), 'attempts': attempts}, wait_history[-64:],
                (root / 'actions').read_text()[-4096:] if (root / 'actions').exists() else 'no action log')
            time.sleep(0.02)
        wait_history.append({'line': line, 'started': round(began-started, 3),
                             'elapsed': round(time.monotonic()-began, 3), 'attempts': attempts})
        del wait_history[:-128]

    def events(instance=''):
        path = root / instance / 'events.jsonl'
        if not path.exists(): return []
        lines = path.read_text().splitlines()
        return [json.loads(line) for line in lines if line.endswith('}')]

    def pixels(color, point=(250, 200)):
        capture_state.clear()
        capture_state.update(point=point, expected=color, attempts=0, started=round(time.monotonic()-started, 3))
        while True:
            capture_state['attempts'] += 1
            (root / 'captured').unlink(missing_ok=True)
            (root / 'capture').touch()
            wait_for(lambda: (root / 'captured').exists())
            with Image.open(root / 'frame.ppm') as frame:
                capture_state['actual'] = frame.getpixel(point)
                if capture_state['actual'] == color:
                    capture_history.append(capture_state | {'elapsed': round(time.monotonic()-started, 3)})
                    del capture_history[:-64]
                    return
            assert time.monotonic() < deadline, ('Provider framebuffer color', capture_state, capture_history, wait_history[-64:])
            time.sleep(0.05)

    def dimensions(instance=''):
        path = root / instance / 'geometry.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def pane_size(pane):
        return {'width': max(320, pane['cols'] * pane['cell_width']) & ~1,
                'height': max(200, (pane['rows'] - 1) * pane['cell_height']) & ~1}

    host = None
    try:
        wait_for(lambda: (root / 'app.json').exists())
        app = json.loads((root / 'app.json').read_text())
        assert app['display'] != env['DISPLAY'] and app['storage'] == str(storage)
        assert app['argv'] == [literal]
        assert app['x11_class'] == 'inherited-x11-app' and app['wayland_class'] == 'inherited-wayland-app'
        assert not (root / 'foreign-storage').exists()
        pixels((34, 68, 102))
        endpoint = (root / 'endpoint').read_text().strip()
        first = request(endpoint, 'list')['panes'][0]['id']
        metadata = lambda identity: request(endpoint, 'info', identity)['panes'][0]
        assert metadata(first)['y'] == 0, 'Shared bottom page-strip setting was not applied'
        wait_for(lambda: dimensions() == pane_size(metadata(first)))
        initial_size = dimensions()
        host = display.Display()
        windows = host.screen().root.query_tree().children
        candidates = [w for w in windows if w.get_attributes().map_state == X.IsViewable
                      and (w.get_geometry().width, w.get_geometry().height) == (640, 480)]
        assert len(candidates) == 1, 'Expected one visible Batty application window'
        window = candidates[0]
        assert window.get_wm_class() == ('batty-kilix', 'batty-kilix'), window.get_wm_class()
        window.set_input_focus(X.RevertToParent, X.CurrentTime)
        host.sync()
        # With every shared pane-button toggle off, the old close-button
        # location belongs to the title. Clicking opens pane actions without
        # closing the pane; Escape returns input to the graphical application.
        geometry = window.get_geometry()
        xtest.fake_input(host, X.MotionNotify, x=geometry.x + 635, y=geometry.y + 5)
        xtest.fake_input(host, X.ButtonPress, 1)
        xtest.fake_input(host, X.ButtonRelease, 1)
        host.sync()
        pixels((34, 68, 102))
        assert len(request(endpoint, 'list')['panes']) == 1
        escape = host.keysym_to_keycode(XK.string_to_keysym('Escape'))
        xtest.fake_input(host, X.KeyPress, escape)
        xtest.fake_input(host, X.KeyRelease, escape)
        host.sync()
        code = host.keysym_to_keycode(XK.string_to_keysym('a'))
        xtest.fake_input(host, X.KeyPress, code)
        xtest.fake_input(host, X.KeyRelease, code)
        geometry = window.get_geometry()
        xtest.fake_input(host, X.MotionNotify, x=geometry.x + 320, y=geometry.y + 240)
        xtest.fake_input(host, X.ButtonPress, 1)
        xtest.fake_input(host, X.ButtonRelease, 1)
        host.sync()
        wait_for(lambda: len(events()) >= 4)
        assert [e['type'] for e in events()[:4]] == [2, 3, 4, 5], events()
        pixels((34, 170, 68))
        # Resize the actual host window. The provider must resize its private
        # display/application, not merely stretch the old screenshot.
        window.configure(width=900, height=600)
        host.sync()
        wait_for(lambda: dimensions() != initial_size and dimensions() == pane_size(metadata(first)))
        pixels((34, 170, 68))
        expanded_size = dimensions()
        # A second provider uses the existing frontend and a separate display.
        command = [str(ROOT / 'kilix'), 'run', '--serve', '--size', '320x240', '--',
                   sys.executable, str(ROOT / 'tests/provider_app.py'), literal, 'second']
        second = request(endpoint, 'launch', first, b'\1' + b'\0'.join(x.encode() for x in command) + b'\0')['id']
        wait_for(lambda: (root / 'second/app.json').exists())
        stream_root = root / 'batty-apps' / 'stream'
        wait_for(lambda: bool(list(stream_root.glob('run-*/connect.txt'))))
        connect_file = list(stream_root.glob('run-*/connect.txt'))[0]
        port = int(next(line for line in connect_file.read_text().splitlines()
                        if line.startswith('VNC')).rsplit(':', 1)[1])
        with socket.create_connection(('127.0.0.1', port), timeout=3) as remote:
            assert remote.recv(12) == b'RFB 003.008\n'
        other = json.loads((root / 'second/app.json').read_text())
        assert other['display'] not in (app['display'], env['DISPLAY'])
        wait_for(lambda: dimensions() != expanded_size and dimensions() == pane_size(metadata(first)))
        wait_for(lambda: dimensions('second') == {'width': 320, 'height': 240})
        def sample(identity):
            pane = metadata(identity)
            return (pane['x'] + pane['width'] // 3, pane['y'] + pane['height'] // 2)
        pixels((34, 170, 68), sample(first))
        pixels((34, 68, 102), sample(second))
        request(endpoint, 'zoom', second)
        pixels((34, 68, 102))
        assert dimensions('second') == {'width': 320, 'height': 240}
        request(endpoint, 'zoom', second)
        pixels((34, 170, 68), sample(first))
        pixels((34, 68, 102), sample(second))
        request(endpoint, 'focus', second)
        xtest.fake_input(host, X.KeyPress, code)
        xtest.fake_input(host, X.KeyRelease, code)
        host.sync()
        wait_for(lambda: len(events('second')) >= 2)
        assert [e['type'] for e in events('second')[:2]] == [2, 3]
        assert len(events()) == 4, 'Keyboard input leaked into the other provider'
        # Exercise the public leader bindings through real host keyboard events
        # and the frontend dispatcher while a graphical application owns input.
        def leader_key(suffix):
            keys = {key: host.keysym_to_keycode(XK.string_to_keysym(key))
                    for key in ('Control_L', 'Shift_L', 'b', suffix)}
            for key in ('Control_L', 'Shift_L', 'b'):
                xtest.fake_input(host, X.KeyPress, keys[key])
            for key in ('b', 'Shift_L', 'Control_L'):
                xtest.fake_input(host, X.KeyRelease, keys[key])
            xtest.fake_input(host, X.KeyPress, keys[suffix])
            xtest.fake_input(host, X.KeyRelease, keys[suffix])
            host.sync()
        leader_key('z')
        wait_for(lambda: not metadata(first)['visible'])
        pixels((34, 68, 102))
        assert dimensions('second') == {'width': 320, 'height': 240}
        leader_key('z')
        wait_for(lambda: metadata(first)['visible'])
        pixels((34, 170, 68), sample(first))
        pixels((34, 68, 102), sample(second))
        original_width = metadata(second)['width']
        leader_key('r')
        right = host.keysym_to_keycode(XK.string_to_keysym('Right'))
        escape = host.keysym_to_keycode(XK.string_to_keysym('Escape'))
        xtest.fake_input(host, X.KeyPress, right)
        xtest.fake_input(host, X.KeyRelease, right)
        host.sync()
        wait_for(lambda: metadata(second)['width'] > original_width)
        xtest.fake_input(host, X.KeyPress, escape)
        xtest.fake_input(host, X.KeyRelease, escape)
        host.sync()
        wait_for(lambda: metadata(second)['width'] == original_width)
        pixels((34, 170, 68), sample(first))
        pixels((34, 68, 102), sample(second))
        # Commit a resize, then reset through the public shortcut while keeping
        # both running applications and their pixels.
        leader_key('r')
        xtest.fake_input(host, X.KeyPress, right)
        xtest.fake_input(host, X.KeyRelease, right)
        enter = host.keysym_to_keycode(XK.string_to_keysym('Return'))
        xtest.fake_input(host, X.KeyPress, enter)
        xtest.fake_input(host, X.KeyRelease, enter)
        host.sync()
        wait_for(lambda: metadata(second)['width'] > original_width)
        reset_keys = [host.keysym_to_keycode(XK.string_to_keysym(key))
                      for key in ('Control_L', 'Shift_L', 'Home')]
        for key in reset_keys:
            xtest.fake_input(host, X.KeyPress, key)
        for key in reversed(reset_keys):
            xtest.fake_input(host, X.KeyRelease, key)
        host.sync()
        wait_for(lambda: metadata(second)['width'] == original_width)
        pixels((34, 170, 68), sample(first))
        pixels((34, 68, 102), sample(second))
        # The real leader pane chooser must switch focus without routing its
        # numeric selection into the embedded graphical application.
        leader_key('q')
        one = host.keysym_to_keycode(XK.string_to_keysym('1'))
        xtest.fake_input(host, X.KeyPress, one)
        xtest.fake_input(host, X.KeyRelease, one)
        host.sync()
        wait_for(lambda: metadata(first)['active'])
        leader_key('q')
        two = host.keysym_to_keycode(XK.string_to_keysym('2'))
        xtest.fake_input(host, X.KeyPress, two)
        xtest.fake_input(host, X.KeyRelease, two)
        host.sync()
        wait_for(lambda: metadata(second)['active'])
        pixels((34, 170, 68), sample(first))
        pixels((34, 68, 102), sample(second))
        # Cycle the actual frontend through every reference layout. Stack
        # keeps only the focused app visible; returning to splits restores it.
        split_bounds = {identity: tuple(metadata(identity)[k] for k in ('x', 'y', 'width', 'height'))
                        for identity in (first, second)}
        for layout in ('stack', 'tall', 'grid', 'splits'):
            leader_key('space')
            wait_for(lambda: metadata(second)['layout'] == layout)
            if layout == 'stack':
                assert not metadata(first)['visible'] and metadata(second)['visible']
                pixels((34, 68, 102))
            else:
                assert metadata(first)['visible'] and metadata(second)['visible']
                pixels((34, 170, 68), sample(first))
                pixels((34, 68, 102), sample(second))
        assert all(tuple(metadata(identity)[k] for k in ('x', 'y', 'width', 'height')) == bounds
                   for identity, bounds in split_bounds.items())
        before_rename = control_events(endpoint)
        leader_key('comma')
        for character in 'work':
            key = host.keysym_to_keycode(XK.string_to_keysym(character))
            xtest.fake_input(host, X.KeyPress, key)
            xtest.fake_input(host, X.KeyRelease, key)
        xtest.fake_input(host, X.KeyPress, enter)
        xtest.fake_input(host, X.KeyRelease, enter)
        host.sync()
        wait_for(lambda: metadata(first)['page_title'] == 'work' and metadata(second)['page_title'] == 'work')
        title_events = control_events(endpoint, before_rename['cursor'], before_rename['epoch'])
        assert any(e['pane'] == first and e['changes'] & 2 for e in title_events['events']), title_events
        assert any(e['pane'] == second and e['changes'] & 2 for e in title_events['events']), title_events
        pixels((34, 170, 68), sample(first))
        pixels((34, 68, 102), sample(second))
        # Closing one live app must reap it and let its sibling expand again.
        request(endpoint, 'close', second)
        wait_for(lambda: not Path('/proc', str(other['pid'])).exists())
        wait_for(lambda: dimensions() == expanded_size)
        pixels((34, 170, 68))
        # Verify the public Kilix default policy reaches the native loadable,
        # and observe the actual desktop clipboard from another X client.
        code = 'import sys;print("\\x1b]52;c;Y2Fmw6k=\\x07CLIP_READY",flush=True);sys.stdin.readline()'
        command = [sys.executable, '-c', code]
        copier = request(endpoint, 'launch', 0, b'\1' + b'\0'.join(x.encode() for x in command) + b'\0')['id']
        wait_for(lambda: 'CLIP_READY' in request(endpoint, 'dump', copier))
        requestor = host.screen().root.create_window(0, 0, 1, 1, 0, X.CopyFromParent)
        property_atom = host.intern_atom('BATTY_TEST_CLIPBOARD')
        utf8 = host.intern_atom('UTF8_STRING')
        requestor.convert_selection(host.intern_atom('CLIPBOARD'), utf8, property_atom, X.CurrentTime)
        host.flush()
        wait_for(lambda: requestor.get_full_property(property_atom, utf8) is not None)
        assert bytes(requestor.get_full_property(property_atom, utf8).value).decode() == 'caf\u00e9'
        requestor.destroy()
        leader_key('w')
        for character in 'work':
            key = host.keysym_to_keycode(XK.string_to_keysym(character))
            xtest.fake_input(host, X.KeyPress, key)
            xtest.fake_input(host, X.KeyRelease, key)
        xtest.fake_input(host, X.KeyPress, enter)
        xtest.fake_input(host, X.KeyRelease, enter)
        host.sync()
        wait_for(lambda: metadata(first)['active'])
        assert 'work' not in request(endpoint, 'dump', copier), 'Chooser search leaked into the terminal'
        pixels((34, 170, 68))
        # Reload supported shared settings through the real frontend binding.
        def reload_settings(expected):
            log = root / 'actions'
            before = log.read_text().count('reload-settings ') if log.exists() else 0
            keys = [host.keysym_to_keycode(XK.string_to_keysym(key))
                    for key in ('Control_L', 'Shift_L', 'F5')]
            for key in keys:
                xtest.fake_input(host, X.KeyPress, key)
            for key in reversed(keys):
                xtest.fake_input(host, X.KeyRelease, key)
            host.sync()
            wait_for(lambda: log.exists() and log.read_text().count('reload-settings ') > before)
            assert log.read_text().splitlines()[-1] == 'reload-settings ' + str(expected)
        original_pid = metadata(first)['pid']
        changed = subprocess.run([str(ROOT / 'kilix'), 'settings', '--set', 'tab_bar_edge=top'],
                                 env=env, capture_output=True, text=True, timeout=5)
        assert changed.returncode == 0, changed.stderr
        reload_settings(0)
        wait_for(lambda: metadata(first)['y'] > 0)
        pixels((34, 170, 68))
        # A rejected settings file must leave the live presentation and PTY.
        saved = settings_file.read_bytes()
        settings_file.unlink()
        os.mkfifo(settings_file)
        reload_settings(1)
        assert metadata(first)['y'] > 0 and metadata(first)['pid'] == original_pid
        settings_file.unlink()
        settings_file.write_bytes(saved.replace(b'TAB_BAR_EDGE=top', b'TAB_BAR_EDGE=bottom'))
        reload_settings(0)
        wait_for(lambda: metadata(first)['y'] == 0)
        assert metadata(first)['pid'] == original_pid
        pixels((34, 170, 68))
        # Open native settings, change edge with Enter, then toggle the first
        # pane-button checkbox with the mouse. Both choices must persist.
        log = root / 'actions'
        opened_before = log.read_text().count('settings 0')
        settings_keys = [host.keysym_to_keycode(XK.string_to_keysym(key))
                         for key in ('Control_L', 'Alt_L', 's')]
        for key in settings_keys:
            xtest.fake_input(host, X.KeyPress, key)
        for key in reversed(settings_keys):
            xtest.fake_input(host, X.KeyRelease, key)
        host.sync()
        wait_for(lambda: log.read_text().count('settings 0') > opened_before)
        xtest.fake_input(host, X.KeyPress, enter)
        xtest.fake_input(host, X.KeyRelease, enter)
        host.sync()
        wait_for(lambda: metadata(first)['y'] > 0)
        assert read_settings(settings_file)['KILIX_CHROME_TAB_BAR_EDGE'] == 'top'
        geometry = window.get_geometry()
        bar_height = metadata(first)['y']
        menu_top = (geometry.height - 11 * bar_height) // 2
        xtest.fake_input(host, X.MotionNotify, x=geometry.x + geometry.width // 2,
                         y=geometry.y + menu_top + 2 * bar_height + 5)
        xtest.fake_input(host, X.ButtonPress, 1)
        xtest.fake_input(host, X.ButtonRelease, 1)
        host.sync()
        wait_for(lambda: 'setting-button-0-on 0' in log.read_text())
        assert read_settings(settings_file)['KILIX_CHROME_BUTTON_SYNCHRONIZE_INPUT'] == 'on'
        assert read_settings(settings_file)['KILIX_CHROME_BUTTON_CLOSE'] == 'off'
        def settings_heading():
            (root / 'captured').unlink(missing_ok=True)
            (root / 'capture').touch()
            wait_for(lambda: (root / 'captured').exists())
            with Image.open(root / 'frame.ppm') as frame:
                left = (frame.width - 640) // 2
                return frame.crop((left, menu_top, left + 640, menu_top + bar_height)).tobytes()
        saved_heading = settings_heading()
        saved_choices = settings_file.read_bytes()
        settings_file.unlink()
        os.mkfifo(settings_file)
        xtest.fake_input(host, X.KeyPress, enter)
        xtest.fake_input(host, X.KeyRelease, enter)
        host.sync()
        wait_for(lambda: 'setting-button-0-off 1' in log.read_text())
        failed_heading = settings_heading()
        assert failed_heading != saved_heading, 'Save failure was not shown in the overlay'
        assert metadata(first)['pid'] == original_pid
        settings_file.unlink()
        settings_file.write_bytes(saved_choices)
        xtest.fake_input(host, X.KeyPress, enter)
        xtest.fake_input(host, X.KeyRelease, enter)
        host.sync()
        wait_for(lambda: 'setting-button-0-off 0' in log.read_text())
        assert read_settings(settings_file)['KILIX_CHROME_BUTTON_SYNCHRONIZE_INPUT'] == 'off'
        assert settings_heading() == saved_heading, 'Successful retry did not clear the error'
        xtest.fake_input(host, X.KeyPress, escape)
        xtest.fake_input(host, X.KeyRelease, escape)
        host.sync()
        pixels((34, 170, 68))
        assert metadata(first)['pid'] == original_pid
        # Open Start with the keyboard and activate a nested pane action.
        menu_keys = [host.keysym_to_keycode(XK.string_to_keysym(key))
                     for key in ('Control_L', 'Alt_L', 'm')]
        for key in menu_keys:
            xtest.fake_input(host, X.KeyPress, key)
        for key in reversed(menu_keys):
            xtest.fake_input(host, X.KeyRelease, key)
        for character in 'al':
            key = host.keysym_to_keycode(XK.string_to_keysym(character))
            xtest.fake_input(host, X.KeyPress, key)
            xtest.fake_input(host, X.KeyRelease, key)
        host.sync()
        wait_for(lambda: metadata(first)['layout'] == 'stack')
        pixels((34, 170, 68))
        # The shared Start badge opens the same menu; its settings action must
        # enter the existing modal without restarting the application.
        before_settings = log.read_text().count('settings 0')
        geometry = window.get_geometry()
        xtest.fake_input(host, X.MotionNotify, x=geometry.x + 20, y=geometry.y + 10)
        xtest.fake_input(host, X.ButtonPress, 1)
        xtest.fake_input(host, X.ButtonRelease, 1)
        key = host.keysym_to_keycode(XK.string_to_keysym('s'))
        xtest.fake_input(host, X.KeyPress, key)
        xtest.fake_input(host, X.KeyRelease, key)
        host.sync()
        wait_for(lambda: log.read_text().count('settings 0') > before_settings)
        xtest.fake_input(host, X.KeyPress, escape)
        xtest.fake_input(host, X.KeyRelease, escape)
        host.sync()
        pixels((34, 170, 68))
        assert metadata(first)['pid'] == original_pid
        # Select the installed terminal application through the actual menu.
        existing_ids = {p['id'] for p in request(endpoint, 'list')['panes']}
        for key in menu_keys:
            xtest.fake_input(host, X.KeyPress, key)
        for key in reversed(menu_keys):
            xtest.fake_input(host, X.KeyRelease, key)
        for symbol in ('g', 'End', 'Return'):
            key = host.keysym_to_keycode(XK.string_to_keysym(symbol))
            xtest.fake_input(host, X.KeyPress, key)
            xtest.fake_input(host, X.KeyRelease, key)
        host.sync()
        wait_for(lambda: (root / 'catalog-launched').exists())
        assert (root / 'catalog-launched').read_text() == literal
        launched = [p for p in request(endpoint, 'list')['panes'] if p['id'] not in existing_ids]
        assert len(launched) == 1 and launched[0]['tab'] != metadata(first)['tab']
        assert launched[0]['active']
        request(endpoint, 'close', launched[0]['id'])
        request(endpoint, 'focus', first)
        pixels((34, 170, 68))
        request(endpoint, 'close', copier)
        (root / 'quit').touch()
        (root / 'stop').touch()
        stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == 0, (stdout, stderr)
        assert not Path('/proc', str(app['pid'])).exists(), 'Provider app survived frontend exit'
        assert not list((root / 'registry').glob('front-*')), 'Frontend registry residue'
        print('Provider timing:', {'seconds': round(time.monotonic()-started, 3),
              'first_frame': capture_history[0]['elapsed'],
              'slowest_waits': sorted(wait_history, key=lambda item: item['elapsed'], reverse=True)[:5]})
    except Exception:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=3)
            print(f'Frontend exit {process.returncode}:\n{stdout}\n{stderr}', file=sys.stderr)
        raise
    finally:
        if host is not None: host.close()
        if process.poll() is None:
            process.terminate()
            try: process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill(); process.communicate()
        # This fixture overrides XDG_RUNTIME_DIR inside its temporary tree,
        # so the suite's outer persistent-owner cleanup cannot see its root.
        sessions = root / 'batty'
        for socket in sessions.glob('kilix-auto-*.sock'):
            subprocess.run([str(ROOT / 'batty'), '--terminate', socket.stem,
                            '--session-dir', str(sessions)],
                           env=env | {'BATTY_CONFIG': '/dev/null'},
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
print('PASS bundled provider: source hashes, public launcher, isolated storage/display, literal argv, framebuffer damage, pane resize/split isolation, real keyboard/mouse and cleanup')
