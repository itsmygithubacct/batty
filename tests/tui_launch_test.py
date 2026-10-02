#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check pinned text-desktop launch isolation and Batty page routing."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import kilix_tui_launch as tui  # noqa: E402

with tempfile.TemporaryDirectory(prefix='bt-tui-launch-') as temporary:
    base = Path(temporary)
    (base / 'storage').mkdir(mode=0o700)
    checkout = base / 'storage/data/desktop-apps/kilix-tui-utils'
    executable = checkout / '.runtime/bin/kilix-tui'
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\n')
    entry = checkout / 'kilix-tui/main.py'
    entry.parent.mkdir()
    entry.write_text('def main(argv): return 0\n')
    env = {'BATTY_KILIX_STORAGE_HOME': str(base / 'storage'),
           'KILIX_HOME': str(base / 'foreign-kilix'),
           'GPU_TERMINAL_SOURCE_HOME': str(base / 'foreign-sources'),
           'KILIX_RC_PASSWORD_FILE': str(base / 'foreign-password')}
    with patch.dict(os.environ, env), \
            patch.object(tui.app, 'ensure_application', return_value=str(executable)), \
            patch.object(tui.os, 'execve') as execute:
        assert tui.main(['--status']) == 0
        child = execute.call_args.args[1]
        selected = execute.call_args.args[2]
        assert child[-2:] == [str(checkout), '--status']
        assert selected['KILIX_HOME'] == str(ROOT / 'third_party/kilix-desktop/src')
        assert selected['GPU_TERMINAL_SOURCE_HOME'] == str(ROOT)
        assert selected['KILIX_STORAGE_HOME'] == str(base / 'storage')
        assert 'KILIX_RC_PASSWORD_FILE' not in selected
    with patch.dict(os.environ, env), \
            patch.object(tui.app, 'ensure_application', return_value=str(executable)), \
            patch.object(tui, 'inside_batty', return_value=True), \
            patch.object(tui.os, 'execv') as execute:
        assert tui.main([]) == 0
        command = execute.call_args.args[1]
        assert command[:6] == [str(ROOT / 'kilix'), 'launch', '--type=tab',
                               '--self', '--tab-title', 'Kilix TUI']
        assert f'GPU_TERMINAL_SOURCE_HOME={ROOT}' in command

print('PASS pinned TUI launch: private package, Batty facts and page routing')
