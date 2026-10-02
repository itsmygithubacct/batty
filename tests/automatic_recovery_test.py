#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Ordinary Kilix panes survive a frontend crash and recover automatically."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from control import request


def wait_for(check, process=None):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        if process is not None and process.poll() is not None:
            raise AssertionError(f'Frontend exited: {process.returncode}')
        time.sleep(0.03)
    raise AssertionError('Automatic recovery deadline')


with tempfile.TemporaryDirectory(prefix='bt-auto-recovery-') as directory:
    base = Path(directory)
    root = base / 'sessions'
    root.mkdir(mode=0o700)
    ready = base / 'ready'
    durable = base / 'state/batty/recovery'
    env = os.environ | {'BATTY_SESSION_DIR': str(root),
                        'BATTY_KILIX_CONFIG': str(ROOT / 'tests/automatic_recovery_config.bash'),
                        'BATTY_AUTO_RECOVERY_READY': str(ready),
                        'XDG_STATE_HOME': str(base / 'state'),
                        'BATTY_KILIX_RECOVERY_DIR': str(durable),
                        'BATTY_OFFLINE': '1'}
    processes = []
    with (base / 'frontends.log').open('w+') as log:
        try:
            def launch(args=(), close=False):
                ready.unlink(missing_ok=True)
                process = subprocess.Popen([str(ROOT / 'kilix'), *args],
                                           env=env | {'BATTY_AUTO_RECOVERY_CLOSE': str(int(close))},
                                           stdout=log, stderr=log)
                processes.append(process)
                wait_for(ready.exists, process)
                endpoint = ready.read_text().strip()
                if not close:
                    wait_for(lambda: request(endpoint, 'ping'), process)
                return process, endpoint

            original, endpoint = launch(('--', '/bin/cat'))
            before = request(endpoint, 'checkpoint')['panes']
            assert len(before) == 1 and before[0]['persistent']
            name, child, epoch = (before[0][key] for key in ('session', 'pid', 'session_epoch'))
            assert name.startswith('kilix-auto-') and child > 0 and int(epoch, 16)
            created = subprocess.run([str(ROOT / 'kilix'), 'new-pane', 'right',
                                      '--socket', endpoint, '--target', str(before[0]['id']),
                                      '--', '/bin/cat'], env=env, capture_output=True,
                                     text=True, timeout=8)
            assert created.returncode == 0, created.stderr
            before = request(endpoint, 'checkpoint')['panes']
            assert len(before) == 2 and all(p['persistent'] for p in before)
            first = next(p for p in before if p['session'] == name)
            other = next(p for p in before if p['session'] != name)
            assert other['session'].startswith('kilix-auto-') and other['pid'] != child
            assert first['tab'] == other['tab']
            request(endpoint, 'rename', first['id'], b'Recovered split')
            before = request(endpoint, 'checkpoint')
            def snapshot_ready():
                for path in durable.glob('.kilix-layout-*.json'):
                    try:
                        saved = json.loads(path.read_text())['checkpoint']
                        if saved['layout_hex'] == before['layout_hex'] and len(saved['panes']) == 2:
                            return True
                    except (OSError, ValueError, KeyError):
                        continue
                return False
            wait_for(snapshot_ready, original)
            request(endpoint, 'send', first['id'], b'AUTO_RECOVERED\n')
            wait_for(lambda: 'AUTO_RECOVERED' in request(endpoint, 'dump', first['id']), original)
            original.kill()
            original.wait(timeout=5)
            assert (root / (name + '.sock')).exists(), 'Owner died with the frontend'
            recovered, endpoint = launch()
            after = request(endpoint, 'checkpoint')['panes']
            assert len(after) == 2
            restored = {p['session']: p for p in after}
            assert restored[name]['pid'] == child and restored[name]['session_epoch'] == epoch
            assert restored[other['session']]['pid'] == other['pid']
            assert restored[name]['tab'] == restored[other['session']]['tab']
            assert restored[name]['page_title'] == 'Recovered split'
            assert restored[name]['x'] < restored[other['session']]['x']
            assert 'AUTO_RECOVERED' in request(endpoint, 'dump', restored[name]['id'])
            subprocess.run([str(ROOT / 'kilix'), 'save', '--socket', endpoint,
                            str(base / 'workspace.json')], env=env, check=True, timeout=8)
            extra = subprocess.run([str(ROOT / 'kilix'), 'new-pane', 'right',
                                    '--socket', endpoint, '--target', str(restored[name]['id']),
                                    '--', '/bin/cat'], env=env, capture_output=True,
                                   text=True, timeout=8)
            assert extra.returncode == 0, extra.stderr
            added = next(p for p in request(endpoint, 'checkpoint')['panes']
                         if p['session'] not in restored)
            request(endpoint, 'close', added['id'])
            wait_for(lambda: not (root / (added['session'] + '.sock')).exists(), recovered)
            recovered.terminate()
            recovered.wait(timeout=5)
            assert (root / (name + '.sock')).exists(), 'Normal frontend exit ended the owner'
            closed, _ = launch(close=True)
            closed.wait(timeout=5)
            assert not (root / (name + '.sock')).exists(), 'Explicit close left the generated owner running'
            assert not (root / (other['session'] + '.sock')).exists(), 'CLI pane owner survived explicit close'
            listing = subprocess.run([str(ROOT / 'batty'), '--list', '--session-dir', str(root)],
                                     env=env, capture_output=True, text=True, check=True, timeout=8).stdout
            selected = subprocess.run([sys.executable, str(ROOT / 'tools/kilix_auto_workspace.py'), '_select'],
                                      env=env, input=listing, capture_output=True, text=True,
                                      check=True, timeout=8)
            assert not selected.stdout.strip(), selected.stdout
            assert not list(durable.glob('.kilix-layout-*.json')), 'Terminated owners left stale layout snapshots'
            print('PASS generated panes recover the split layout, title, PIDs and output; closed owners leave no snapshots')
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read(), file=sys.stderr)
            raise
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            for socket in root.glob('kilix-auto-*.sock'):
                subprocess.run([str(ROOT / 'batty'), '--terminate', socket.stem,
                                '--session-dir', str(root)], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
