#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A catalog-style Batty window owns its child until the window closes."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from control import request


def wait_for(check, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('ephemeral app deadline')


with tempfile.TemporaryDirectory(prefix='bt-ephemeral-app-') as directory:
    root = Path(directory)
    marker = root / 'child-pid'
    child = root / 'child.py'
    child.write_text('import os,sys,time\n'
                     'from pathlib import Path\n'
                     'Path(sys.argv[1]).write_text(str(os.getpid()))\n'
                     'print("EPHEMERAL_APP_READY",flush=True)\n'
                     'while True: time.sleep(1)\n')
    env = os.environ | {'BATTY_CONTROL_DIR': str(root / 'registry'),
                        'BATTY_SESSION_DIR': str(root / 'sessions'),
                        'XDG_STATE_HOME': str(root / 'state'),
                        'PYTHONDONTWRITEBYTECODE': '1',
                        'BATTY_KILIX_AUTO_RECOVER': '1'}
    with (root / 'frontend.log').open('wb') as log:
        frontend = subprocess.Popen([str(ROOT / 'kilix'), '--ephemeral', '--',
                                     sys.executable, str(child), str(marker)],
                                    cwd=ROOT, env=env, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        child_pid = None
        try:
            def current_pane():
                for endpoint in (root / 'registry').glob('front-*/control.sock'):
                    try:
                        panes = request(str(endpoint), 'list', timeout=.3)['panes']
                        if panes:
                            return endpoint, panes[0]
                    except (OSError, ValueError, RuntimeError):
                        pass
                return None

            endpoint, pane = wait_for(current_pane)
            child_pid = int(wait_for(lambda: marker.read_text() if marker.exists() else None))
            assert pane['pid'] == child_pid and not pane['persistent']
            assert pane['session'] == '' and pane['session_dir'] == ''
            wait_for(lambda: 'EPHEMERAL_APP_READY' in request(
                str(endpoint), 'dump', pane['id']))
            assert not list((root / 'sessions').glob('*.sock'))
            assert not list((root / 'sessions').glob('.kilix-layout-*.json'))
            frontend.send_signal(signal.SIGTERM)
            assert frontend.wait(timeout=6) == 143
            wait_for(lambda: not Path('/proc', str(child_pid)).exists(), timeout=6)
            assert not list((root / 'sessions').glob('*.sock'))
        finally:
            if frontend.poll() is None:
                os.killpg(frontend.pid, signal.SIGKILL)
                frontend.wait(timeout=5)
            if child_pid is not None and Path('/proc', str(child_pid)).exists():
                os.kill(child_pid, signal.SIGKILL)

print('PASS ephemeral Batty app window: live PTY, no owner/snapshot and child exits with frontend')
