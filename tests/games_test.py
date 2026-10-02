#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check the shared game vocabulary and Batty's host-side toggles."""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent

with tempfile.TemporaryDirectory(prefix='bt-games-') as temporary:
    base = Path(temporary)
    env = os.environ | {'GPU_TERMINAL_SETTINGS_FILE': str(base / 'settings.conf'),
                        'BATTY_KILIX_STORAGE_HOME': str(base / 'storage')}

    def run(*args):
        return subprocess.run([str(ROOT / 'kilix'), 'games', *args], env=env,
                              capture_output=True, text=True, timeout=10)

    assert 'play GAME' in run('help').stdout
    initial = run('list')
    assert initial.returncode == 0
    assert 'doom enabled Doom' in initial.stdout
    assert 'minesweeper enabled Minesweeper' in initial.stdout
    assert run('disable', 'doom', 'minesweeper').returncode == 0
    assert 'doom disabled Doom' in run('list').stdout
    denied = run('play', 'doom', '--setup-only')
    assert denied.returncode == 1 and 'doom is disabled' in denied.stderr
    assert run('install', 'minesweeper').returncode == 0
    assert 'minesweeper disabled' in run('list').stdout
    assert run('enable', 'doom', 'minesweeper').returncode == 0
    assert 'doom enabled Doom' in run('list').stdout
    assert run('play', 'minesweeper', '--setup-only').returncode == 0
    assert run('play', 'not-a-game').returncode == 1
