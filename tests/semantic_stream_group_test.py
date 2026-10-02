#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Two independent remote owners in one native Kilix workspace."""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from control import request
from kilix_stream_server import StreamServer

root = Path(os.environ['BATTY_TEST_SESSION_DIR'])
mirror_root = root / 'mirrors'
control_root = root / 'control'
control_root.mkdir(mode=0o700)
env = os.environ | {'BATTY_CONFIG': '/dev/null', 'BATTY_KILIX_CONFIG': '/dev/null',
                    'BATTY_CONTROL_DIR': str(control_root)}
owners = []
servers = []
threads = []
viewer = None
log = root / 'group-viewer.log'


def until(check, label, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        assert viewer is None or viewer.poll() is None, (label, log.read_text() if log.exists() else '')
        time.sleep(0.03)
    raise AssertionError((label, log.read_text() if log.exists() else ''))


try:
    for name, marker in (('group-one', 'GROUP_ONE'), ('group-two', 'GROUP_TWO')):
        code = (f'import time; print("{marker}",flush=True); '
                'print("ECHO:"+input(),flush=True); time.sleep(30)')
        owner = subprocess.Popen([str(ROOT / 'batty'), '--headless', '--session', name,
                                  '--session-dir', str(root), '--', sys.executable, '-u', '-c', code],
                                 cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        owners.append((name, owner))
        until(lambda: (root / f'{name}.sock').exists(), f'{name} owner startup')
        server = StreamServer(root, name)
        servers.append(server)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)
    routes = [f'{servers[0].port},{servers[0].view_token.hex()},{servers[0].input_port},observe',
              f'{servers[1].port},{servers[1].token.hex()},{servers[1].input_port}']
    rejected = subprocess.run([str(ROOT / 'kilix'), 'remote', 'view-group',
                               '--session-dir', str(mirror_root), '--name', 'bad-group',
                               '--route', routes[0],
                               '--route', f'{servers[1].port},{"0" * 32},{servers[1].input_port}'],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=12)
    assert rejected.returncode != 0 and not list(mirror_root.glob('bad-group*.sock')), (
        'Rejected group did not clean up its mirrors', rejected.stderr)
    with log.open('wb') as output:
        viewer = subprocess.Popen([str(ROOT / 'kilix'), 'remote', 'view-group',
                                   '--session-dir', str(mirror_root), '--name', 'two-panes',
                                   '--route', routes[0], '--route', routes[1]],
                                  cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
        endpoint = until(lambda: next(control_root.glob('front-*/control.sock'), None),
                         'group frontend startup')

        def panes_ready():
            try:
                panes = request(str(endpoint), 'list')['panes']
                return panes if len(panes) == 2 else None
            except (OSError, RuntimeError):
                return None

        panes = until(panes_ready, 'two remote workspace panes')
        assert {p['observe'] for p in panes} == {True, False}, panes
        observer = next(p for p in panes if p['observe'])
        controller = next(p for p in panes if not p['observe'])
        last_dumps = {}

        def both_visible():
            try:
                last_dumps['observer'] = request(str(endpoint), 'dump', observer['id'])
                last_dumps['controller'] = request(str(endpoint), 'dump', controller['id'])
                return 'GROUP_ONE' in last_dumps['observer'] and 'GROUP_TWO' in last_dumps['controller']
            except (OSError, RuntimeError) as error:
                last_dumps['error'] = str(error)
                return False

        try:
            until(both_visible, 'independent remote text in both panes')
        except AssertionError as error:
            raise AssertionError((error, panes, last_dumps, [owner.poll() for _, owner in owners])) from error
        try:
            request(str(endpoint), 'send', observer['id'], b'forbidden\n')
        except RuntimeError as error:
            assert 'Observer' in str(error), error
        else:
            raise AssertionError('Read-only remote pane accepted input')
        request(str(endpoint), 'send', controller['id'], b'GROUP_INPUT\n')
        until(lambda: 'ECHO:GROUP_INPUT' in request(str(endpoint), 'dump', controller['id']),
              'remote input to the selected pane')
        assert 'ECHO:GROUP_INPUT' not in request(str(endpoint), 'dump', observer['id'])
        assert all(owner.poll() is None for _, owner in owners)
finally:
    if viewer is not None:
        viewer.terminate()
        try:
            viewer.wait(timeout=7)
        except subprocess.TimeoutExpired:
            viewer.kill()
            viewer.wait(timeout=3)
    for server in servers:
        server.close()
    for thread in threads:
        thread.join(timeout=3)
    for name, owner in owners:
        owner.terminate()
        try:
            owner.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            owner.kill()
            owner.communicate(timeout=3)
        subprocess.run([str(ROOT / 'batty'), '--terminate', name, '--session-dir', str(root)],
                       cwd=ROOT, env=env, capture_output=True, timeout=8, check=True)

assert not list(mirror_root.glob('*.sock')), 'Grouped viewer left mirror endpoints'
print('PASS two semantic remote panes, mixed input roles, live text and cleanup in one Kilix workspace')
