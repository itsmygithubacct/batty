#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check the TUI status report uses Batty's runtime and control endpoint."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import kilix_status  # noqa: E402

with tempfile.TemporaryDirectory(prefix='bt-status-') as temporary:
    env = os.environ | {'BATTY_KILIX_STORAGE_HOME': str(Path(temporary) / 'storage')}
    result = subprocess.run([str(ROOT / 'kilix'), 'status', '--json'], env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['host'] == 'Batty Kilix'
    assert report['default_desktop'] == 'none'
    assert report['frontend'] is None
    assert report['bash_os_revision'] == json.loads((ROOT / 'build/bash-os.json').read_text())['revision']
    panes = [{'tab': 1, 'exit_status': None}, {'tab': 1, 'exit_status': 0},
             {'tab': 2, 'exit_status': None}]
    with patch.dict(os.environ, env | {'BATTY_CONTROL': '/private/control.sock'}), \
            patch.object(kilix_status, 'resolve_endpoint', return_value='/private/control.sock'), \
            patch.object(kilix_status, 'request', return_value={'panes': panes}):
        assert kilix_status.status()['frontend'] == {'pages': 2, 'panes': 3, 'running': 2}

print('PASS Batty status: verified runtime, private default and attached pane counts')
