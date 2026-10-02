#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise the TUI software-menu contract without installing content."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import kilix_install  # noqa: E402

with tempfile.TemporaryDirectory(prefix='bt-install-') as temporary:
    env = os.environ | {'BATTY_KILIX_STORAGE_HOME': str(Path(temporary) / 'storage'),
                        'BATTY_OFFLINE': '1'}
    listed = subprocess.run([str(ROOT / 'kilix'), 'install', '--json'], env=env,
                            capture_output=True, text=True, timeout=20)
    assert listed.returncode == 0, listed.stderr
    rows = json.loads(listed.stdout)
    assert {row['id'] for row in rows} == {
        entry.content_id for entry in kilix_install.app.default_catalog()
        if entry.kind in ('app', 'game')}
    assert all(set(row) == {'id', 'label', 'kind', 'description', 'installed'}
               and type(row['installed']) is bool for row in rows)
    assert all(not row['installed'] for row in rows)
    assert subprocess.run([str(ROOT / 'kilix'), 'install', 'unknown'], env=env,
                          capture_output=True, timeout=5).returncode != 0
    for content_id, action in (('kilix-file', ['app', 'install', 'kilix-file']),
                               ('doom', ['games', 'install', 'doom'])):
        with patch.object(kilix_install.os, 'execv') as execute:
            assert kilix_install.main([content_id]) == 0
            assert execute.call_args.args[1] == [str(ROOT / 'kilix'), *action]

print('PASS pinned install list: TUI JSON contract, private readiness and explicit dispatch')
