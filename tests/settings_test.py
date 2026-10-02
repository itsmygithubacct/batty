#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Shared settings are bounded data, never executable configuration."""
import os
from pathlib import Path
import sys
import subprocess
import fcntl
import time
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from kilix_settings import start_menu, BUTTONS, pane_buttons, LIMIT, TRANSCRIPTS, recording_policy, transcript_value, read_settings, settings_path, tab_bar_edge

with tempfile.TemporaryDirectory(prefix='bt-settings-') as directory:
    root = Path(directory)
    path = root / 'settings.conf'
    assert not start_menu({})
    assert start_menu({'KILIX_CHROME_START_MENU': 'on'})
    assert not start_menu({'KILIX_CHROME_START_MENU': 'false'})
    assert pane_buttons({}) == 511
    assert pane_buttons({'KILIX_CHROME_BUTTON_' + name: 'off' for name in BUTTONS}) == 0
    assert pane_buttons({'KILIX_CHROME_BUTTON_CLOSE': 'OFF', 'KILIX_CHROME_BUTTON_FONT_DECREASE': 'false'}) == 253
    for value in ('', '0', 'no', 'false', 'off', 'disabled'):
        assert pane_buttons({'KILIX_CHROME_BUTTON_CLOSE': value}) == 255
    with patch.dict(os.environ, {'GPU_TERMINAL_SETTINGS_FILE': str(path), 'BATTY_TAB_BAR_EDGE': ''}):
        assert read_settings(path) == {} and tab_bar_edge() == 'top'
        path.write_text('KILIX_CHROME_TAB_BAR_EDGE=top\nKILIX_CHROME_TAB_BAR_EDGE=BOTTOM\n')
        assert tab_bar_edge() == 'bottom'
        path.write_text('KILIX_CHROME_TAB_BAR_EDGE=$(touch ' + str(root / 'executed') + ')\n')
        assert tab_bar_edge() == 'top' and not (root / 'executed').exists()
        with patch.dict(os.environ, {'BATTY_TAB_BAR_EDGE': 'bottom'}):
            assert tab_bar_edge() == 'bottom'
        with patch.dict(os.environ, {'BATTY_TAB_BAR_EDGE': 'invalid'}):
            try: tab_bar_edge()
            except ValueError: pass
            else: raise AssertionError('Invalid explicit override accepted')
        path.write_bytes(b'x' * (LIMIT + 1))
        try: read_settings(path)
        except ValueError: pass
        else: raise AssertionError('Oversized settings accepted')
        path.unlink()
        os.mkfifo(path)
        try: read_settings(path)
        except ValueError: pass
        else: raise AssertionError('FIFO accepted')
        path.unlink()
        target = root / 'target'
        target.write_text('KILIX_CHROME_TAB_BAR_EDGE=bottom\n')
        path.symlink_to(target)
        try: read_settings(path)
        except OSError: pass
        else: raise AssertionError('Symlink accepted')
    with patch.dict(os.environ, {'GPU_TERMINAL_SETTINGS_FILE': '', 'GPU_TERMINAL_HOME': directory}):
        assert settings_path() == path
with tempfile.TemporaryDirectory(prefix='bt-settings-write-') as directory:
    root = Path(directory)
    path = root / 'settings.conf'
    launcher = Path(__file__).resolve().parents[1] / 'kilix'
    env = os.environ | {'GPU_TERMINAL_SETTINGS_FILE': str(path)}
    def cli(*args, code=0):
        result = subprocess.run([str(launcher), 'settings', *args], env=env,
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == code, (args, result.stdout, result.stderr)
        return result.stdout
    def displayed_recording():
        result = subprocess.run([sys.executable, str(launcher.parent / 'tools/kilix_settings.py'),
                                 '--recording-settings'], env=env, capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip().split()
    assert displayed_recording() == ['on', '8M', 'elide', '5G', '1G']
    assert cli('--get', 'tab_bar_edge').strip() == 'top' and not path.exists()
    for name, (_, default, choices) in TRANSCRIPTS.items():
        assert cli('--get', name).strip() == default
        for choice in choices:
            cli('--set', name + '=' + choice.lower())
            assert cli('--get', name).strip() == choice
        saved = path.read_bytes()
        cli('--set', name + '=$(false)', code=1)
        assert path.read_bytes() == saved
    assert displayed_recording() == [choices[-1] for _, _, choices in TRANSCRIPTS.values()]
    path.unlink()
    with patch.dict(os.environ, {'BATTY_TRANSCRIPT_LIMIT': '', 'BATTY_TRANSCRIPT_GRAPHICS': ''}):
        assert recording_policy({}) == (1, 8388608, 'elide')
        assert recording_policy({'KILIX_TRANSCRIPT': 'off', 'KILIX_TRANSCRIPT_MAX_SIZE': '32M',
                                 'KILIX_TRANSCRIPT_GRAPHICS': 'keep'}) == (0, 33554432, 'keep')
        assert transcript_value({'KILIX_TRANSCRIPT_MAX_TOTAL': 'garbage'}, 'transcript_total') == '5G'
        with patch.dict(os.environ, {'BATTY_TRANSCRIPT_LIMIT': '4096', 'BATTY_TRANSCRIPT_GRAPHICS': 'keep'}):
            assert recording_policy({}) == (1, 8388608, 'elide')
        for name, value in (('BATTY_TRANSCRIPT_LIMIT', '1'), ('BATTY_TRANSCRIPT_GRAPHICS', 'invalid')):
            with patch.dict(os.environ, {name: value}):
                assert recording_policy({}) == (1, 8388608, 'elide'), 'Shared preset reader must not interpret explicit override bytes'
    original = b'# private comment \xff\r\nUNKNOWN=keep\r\nKILIX_CHROME_TAB_BAR_EDGE=top\r\nKILIX_CHROME_TAB_BAR_EDGE=top\r\n'
    path.write_bytes(original)
    cli('--set', 'tab_bar_edge=bottom', '--set', 'button_close=off')
    changed = path.read_bytes()
    assert changed.startswith(original.rsplit(b'KILIX_CHROME_TAB_BAR_EDGE=top', 1)[0])
    assert b'KILIX_CHROME_TAB_BAR_EDGE=bottom\r\n' in changed
    assert cli('--get', 'tab_bar_edge').strip() == 'bottom'
    assert cli('--get', 'KILIX_CHROME_BUTTON_CLOSE').strip() == 'off'
    assert path.stat().st_mode & 0o777 == 0o600
    cli('--set', 'tab_bar_edge=top', '--set', 'button_close=$(false)', code=1)
    assert path.read_bytes() == changed, 'Invalid batch partially changed settings'
    cli('--set', 'unsupported=on', code=1)
    assert path.read_bytes() == changed
    # Hold the same lock as the reference SDK and prove the CLI waits for it.
    with open(str(path) + '.lock', 'r+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        writer = subprocess.Popen([str(launcher), 'settings', '--set', 'tab_bar_edge=top'], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(0.1)
            assert writer.poll() is None and path.read_bytes() == changed
            fcntl.flock(lock, fcntl.LOCK_UN)
            out, err = writer.communicate(timeout=5)
            assert writer.returncode == 0, (out, err)
        finally:
            if writer.poll() is None:
                writer.kill(); writer.communicate()
    writers = [subprocess.Popen([str(launcher), 'settings', '--set', 'button_' + name.lower() + '=off'],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for name in BUTTONS]
    try:
        for writer in writers:
            out, err = writer.communicate(timeout=5)
            assert writer.returncode == 0, (out, err)
    finally:
        for writer in writers:
            if writer.poll() is None:
                writer.kill(); writer.communicate()
    assert pane_buttons(read_settings(path)) == 0, 'Concurrent update lost a setting'
    assert b'# private comment \xff\r\nUNKNOWN=keep\r\n' in path.read_bytes()
    path.write_bytes(b'#' + b'x' * (LIMIT - 1))
    cli('--set', 'tab_bar_edge=bottom', code=1)
    assert path.stat().st_size == LIMIT
    path.unlink()
    target = root / 'target'
    target.write_bytes(b'UNCHANGED=yes\n')
    path.symlink_to(target)
    cli('--set', 'tab_bar_edge=bottom', code=1)
    assert target.read_bytes() == b'UNCHANGED=yes\n' and path.is_symlink()
    assert not list(root.glob('.settings.conf.*')), 'Temporary settings files leaked'
print('PASS shared settings: bounded data parsing, atomic CLI updates, preservation, shared locking and concurrent writers')
