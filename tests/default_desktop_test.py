#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check the persistent desktop choice used by later bare Kilix launches."""
import os
from pathlib import Path
import stat
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory(prefix='bt-default-desktop-') as temporary:
    storage = Path(temporary) / 'storage'
    env = os.environ | {'BATTY_KILIX_STORAGE_HOME': str(storage)}

    def cli(*args):
        return subprocess.run([str(ROOT / 'kilix'), 'default-desktop', *args],
                              env=env, capture_output=True, text=True, timeout=5)

    assert cli('show').stdout.strip() == 'none'
    assert {'none', '95', 'xp'}.issubset(set(cli('list').stdout.splitlines()))
    assert cli('set', 'xp').returncode == 0
    selected = storage / 'config/default-desktop'
    assert selected.read_text() == 'xp\n'
    assert stat.S_IMODE(selected.stat().st_mode) == 0o600
    assert cli('show').stdout.strip() == 'xp'
    assert cli('_startup').stdout.strip() == 'xp'
    assert cli('set', 'cap').returncode == 0
    assert cli('_startup').stdout.strip() == 'cap'
    assert cli('set', 'land').returncode == 0
    assert cli('_startup').stdout.strip() == 'land'
    assert cli('show').stdout.strip() == 'land'
    assert cli('set', 'icewm').returncode == 0
    assert cli('_startup').stdout.strip() == 'icewm'
    assert cli('set', 'auto').returncode == 0
    assert cli('_startup').stdout.strip() == '95'
    assert cli('set', 'tui').returncode == 0
    assert cli('_startup').stdout.strip() == 'tui'
    selected.unlink()
    selected.symlink_to(Path(temporary) / 'foreign')
    assert cli('show').returncode != 0

print('PASS default desktop: private atomic choice, startup mapping and unsafe-file refusal')
