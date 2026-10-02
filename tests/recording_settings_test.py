#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Shared recording policy reaches new owners without altering surviving owners."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from Xlib import X, XK, display
from Xlib.ext import xtest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from control import request


def wait_for(check):
    deadline = time.monotonic() + 7
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError('Recording settings deadline')


with tempfile.TemporaryDirectory(prefix='bt-recording-settings-') as directory:
    base = Path(directory)
    logs = base / 'logs'; logs.mkdir(mode=0o700)
    settings = base / 'settings.conf'
    env = {k: v for k, v in os.environ.items() if k not in ('BATTY_TRANSCRIPT_LIMIT', 'BATTY_TRANSCRIPT_GRAPHICS')}
    env.update(BATTY_TRANSCRIPT_DIR=str(logs), BATTY_TRANSCRIPT_MAINTENANCE='0', GPU_TERMINAL_SETTINGS_FILE=str(settings),
               BATTY_KILIX_CONFIG=str(ROOT / 'tests/recording_config.bash'),
               BATTY_SESSION_DIR=os.environ['BATTY_TEST_SESSION_DIR'], BATTY_CONTROL_DIR=str(base / 'control'),
               BATTY_KILIX_AUTO_RECOVER='0')
    processes = []
    def configure(*changes):
        subprocess.run([str(ROOT / 'kilix'), 'settings', *sum((['--set', c] for c in changes), [])],
                       env=env, check=True, capture_output=True, timeout=5)
    def start(label, arguments, overrides=None):
        ready = base / (label + '.ready')
        log = (base / (label + '.out')).open('w')
        process = subprocess.Popen([str(ROOT / 'kilix'), *arguments], env=env | {'BATTY_RECORDING_READY': str(ready)} | (overrides or {}),
                                   stdout=log, stderr=log)
        processes.append((process, log))
        endpoint = wait_for(lambda: ready.read_text().strip() if ready.exists() else None)
        pane = request(endpoint, 'list')['panes'][0]
        return process, endpoint, pane
    try:
        configure('transcript=on', 'transcript_size=32M', 'transcript_graphics=keep')
        first, endpoint, pane = start('first', ['--session', 'recording-settings', '--', '/bin/cat'])
        assert pane['recording'] == 'active'
        identity = pane['transcript_id']
        assert re.fullmatch('[0-9a-f]{32}', identity), identity
        assert pane['transcript_dir'] == str(logs)
        child = pane['pid']
        exported = (Path('/proc') / str(child) / 'environ').read_bytes().split(b'\0')
        assert b'BATTY_TRANSCRIPT_LIMIT=33554432' in exported
        assert b'BATTY_TRANSCRIPT_GRAPHICS=keep' in exported
        request(endpoint, 'send', pane['id'], b'BEFORE_SETTINGS_CHANGE\n')
        transcript = wait_for(lambda: next(iter(logs.glob('*.log')), None))
        assert transcript.name == identity + '.log'
        wait_for(lambda: b'BEFORE_SETTINGS_CHANGE' in transcript.read_bytes())
        # Change every shared recording preset through the real Kilix window.
        host = display.Display()
        try:
            def recording_window():
                for window in host.screen().root.query_tree().children:
                    if (window.get_attributes().map_state == X.IsViewable and
                            window.get_wm_class() == ('batty-kilix', 'batty-kilix')):
                        return window
                return None
            window = wait_for(recording_window)
            window.set_input_focus(X.RevertToParent, X.CurrentTime)
            host.sync()
            def key(name):
                code = host.keysym_to_keycode(XK.string_to_keysym(name))
                assert code, name
                xtest.fake_input(host, X.KeyPress, code)
                xtest.fake_input(host, X.KeyRelease, code)
                host.sync()
            ctrl = host.keysym_to_keycode(XK.string_to_keysym('Control_L'))
            alt = host.keysym_to_keycode(XK.string_to_keysym('Alt_L'))
            setting = host.keysym_to_keycode(XK.string_to_keysym('s'))
            xtest.fake_input(host, X.KeyPress, ctrl)
            xtest.fake_input(host, X.KeyPress, alt)
            xtest.fake_input(host, X.KeyPress, setting)
            xtest.fake_input(host, X.KeyRelease, setting)
            xtest.fake_input(host, X.KeyRelease, alt)
            xtest.fake_input(host, X.KeyRelease, ctrl)
            host.sync()
            key('End')
            key('Return')
            wait_for(lambda: 'KILIX_TRANSCRIPT_ARCHIVE_MAX_TOTAL=5G' in settings.read_text())
            key('Up'); key('Return')
            wait_for(lambda: 'KILIX_TRANSCRIPT_MAX_TOTAL=10G' in settings.read_text())
            key('Up'); key('Return')
            wait_for(lambda: 'KILIX_TRANSCRIPT_GRAPHICS=elide' in settings.read_text())
            key('Up'); key('Return')
            wait_for(lambda: 'KILIX_TRANSCRIPT_MAX_SIZE=128M' in settings.read_text())
            key('Up'); key('Return')
            wait_for(lambda: 'KILIX_TRANSCRIPT=off' in settings.read_text())
            key('Escape')
        finally:
            host.close()
        assert request(endpoint, 'info', pane['id'])['panes'][0]['recording'] == 'active'
        second, disabled_endpoint, disabled = start('disabled', ['--', '/bin/cat'])
        assert (disabled['recording'] == 'disabled' and disabled['transcript_id'] is None
                and disabled['transcript_dir'] is None)
        missing = subprocess.run([str(ROOT / 'kilix'), 'transcript', 'show', '--pane', str(disabled['id']),
                                  '--socket', disabled_endpoint], env=env, capture_output=True, timeout=5)
        assert missing.returncode != 0 and b'no available transcript' in missing.stderr
        request(disabled_endpoint, 'send', disabled['id'], b'DISABLED_OUTPUT\n')
        # Close the original view, then attach under the new disabled setting.
        request(endpoint, 'close', pane['id'])
        first.wait(timeout=5)
        other_logs = base / 'other-logs'; other_logs.mkdir(mode=0o700)
        replacement, endpoint, attached = start('replacement', ['--attach', 'recording-settings'],
                                                {'BATTY_TRANSCRIPT_DIR': str(other_logs)})
        assert attached['pid'] == child and attached['recording'] == 'active'
        assert attached['transcript_id'] == identity and attached['transcript_dir'] == str(logs)
        request(endpoint, 'send', attached['id'], b'AFTER_SETTINGS_CHANGE\n')
        wait_for(lambda: b'AFTER_SETTINGS_CHANGE' in transcript.read_bytes())
        pane_args = ['--pane', str(attached['id']), '--socket', endpoint]
        resolved = subprocess.run([str(ROOT / 'kilix'), 'transcript', 'path', *pane_args],
                                  env=env | {'BATTY_TRANSCRIPT_DIR': str(other_logs)},
                                  capture_output=True, timeout=5)
        assert resolved.returncode == 0 and resolved.stdout.decode().strip() == str(transcript), resolved.stderr
        shown = subprocess.run([str(ROOT / 'kilix'), 'transcript', 'show', *pane_args],
                               env=env | {'BATTY_TRANSCRIPT_DIR': str(other_logs)},
                               capture_output=True, timeout=5)
        assert shown.returncode == 0 and b'AFTER_SETTINGS_CHANGE' in shown.stdout, shown.stderr
        assert len(list(logs.glob('*.log'))) == 1
        assert b'DISABLED_OUTPUT' not in transcript.read_bytes()
        # Settings are also read by maintenance; explicit flags retain priority.
        configure('transcript_total=1G', 'transcript_archive_total=off')
        result = subprocess.run([str(ROOT / 'kilix'), 'transcript', 'archive'], env=env,
                                capture_output=True, timeout=5)
        assert result.returncode != 0 and b'nonzero archive budget' in result.stderr
        result = subprocess.run([str(ROOT / 'kilix'), 'transcript', 'archive', '--archive-budget', '1M'],
                                env=env, capture_output=True, timeout=5)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['protected'] == 1
        request(disabled_endpoint, 'close', disabled['id']); second.wait(timeout=5)
        request(endpoint, 'close', attached['id']); replacement.wait(timeout=5)
        configure('transcript=on')
        failed, endpoint, pane = start('invalid', ['--', '/bin/cat'],
                                       {'BATTY_TRANSCRIPT_LIMIT': '$(touch ' + str(base / 'executed') + ')'})
        wait_for(lambda: request(endpoint, 'list')['panes'][0]['recording'] == 'failed')
        request(endpoint, 'send', pane['id'], b'RECORDER_FAILURE_TERMINAL_OK\n')
        wait_for(lambda: 'RECORDER_FAILURE_TERMINAL_OK' in request(endpoint, 'dump', pane['id']))
        assert not (base / 'executed').exists()
        request(endpoint, 'close', pane['id']); failed.wait(timeout=5)
    finally:
        for process, log in processes:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
            log.close()
        subprocess.run([str(ROOT / 'batty'), '--terminate', 'recording-settings', '--session-dir', env['BATTY_SESSION_DIR']],
                       env=env, capture_output=True, timeout=10)
print('PASS recording settings: native overlay updates five shared presets, new-window policy, surviving owner invariance and retention overrides')
