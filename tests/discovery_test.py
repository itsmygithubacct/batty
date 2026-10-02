#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Persistent child commands follow their controlling view across frontends."""
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from control import request


def wait_for_file(path, process):
    deadline = time.monotonic() + 8
    while not path.exists() or not path.stat().st_size:
        assert process.poll() is None, process.returncode
        assert time.monotonic() < deadline, str(path)
        time.sleep(0.01)
    return path.read_text().strip()


with tempfile.TemporaryDirectory(prefix='bt-discovery-') as directory:
    base = Path(directory)
    registry = base / 'frontends'
    env = os.environ | {'BATTY_CONTROL_DIR': str(registry),
                        'BATTY_SESSION_DIR': os.environ['BATTY_TEST_SESSION_DIR'],
                        'BATTY_KILIX_CONFIG': str(ROOT / 'tests/discovery_config.bash')}
    processes, logs = [], []
    def start(name, arguments):
        ready = base / f'{name}.ready'
        log = (base / f'{name}.log').open('w+')
        logs.append(log)
        process = subprocess.Popen([str(ROOT / 'kilix'), *arguments], cwd=ROOT,
                                   env=env | {'BATTY_DISCOVERY_ENDPOINT': str(ready)}, stdout=log, stderr=log)
        processes.append(process)
        return process, wait_for_file(ready, process)
    def stop(process):
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            task = Path(f'/proc/{process.pid}')
            try:
                state = task.joinpath('status').read_text()
                state = '\n'.join(line for line in state.splitlines() if line.startswith(('State:', 'Threads:', 'PPid:')))
                waits = [(entry.name, entry.joinpath('wchan').read_text().strip())
                         for entry in task.joinpath('task').iterdir()]
                print(f'Frontend {process.pid} did not stop: {state}; waits={waits}', file=sys.stderr)
            except OSError as error:
                print(f'Frontend {process.pid} shutdown inspection failed: {error}', file=sys.stderr)
            raise
    try:
        first, old = start('first', ['--session', 'discovery', '--', sys.executable,
                                   str(ROOT / 'tests/discovery_child.py'), directory])
        original = request(old, 'list')['panes'][0]
        other = request(old, 'launch', 0, b'\1/bin/cat\0')['id']
        request(old, 'pane-center')
        deadline = time.monotonic() + 5
        while True:
            overlay = request(old, 'info', other)['panes'][0]
            if overlay['overlay_activity'] == 'running' and overlay['overlay_process'] == 'cat':
                break
            assert first.poll() is None and time.monotonic() < deadline, overlay
            time.sleep(0.05)
        current = next(p['id'] for p in request(old, 'list')['panes'] if p['active'])
        request(old, 'focus', original['id'] if current != original['id'] else other)
        assert not request(old, 'pane-center-state')['open']
        request(old, 'close', original['id'])
        second, replacement = start('second', ['--attach', 'discovery'])
        spectator, spectator_path = start('spectator', ['--observe', 'discovery'])
        assert request(spectator_path, 'list')['panes'][0]['observe']
        # A crashed endpoint leaves a socket inode but must not block discovery.
        stale = registry / 'front-stale'
        stale.mkdir(mode=0o700)
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as abandoned:
            abandoned.bind(str(stale / 'control.sock'))
        (stale / 'control.sock').chmod(0o600)
        assert old != replacement
        attached = request(replacement, 'list')['panes'][0]
        assert attached['pid'] == original['pid']
        request(replacement, 'send', attached['id'], b'1\n')
        report = json.loads(wait_for_file(base / 'report-1.json', second))
        assert report['endpoint'] == replacement and report['inherited'] == old
        for tool in ('kilix', 'battyctl'):
            assert [p['pid'] for p in report[tool]['panes']] == [original['pid']]
        assert request(old, 'list')['panes'][0]['id'] == other  # old frontend still lives
        stop(first)
        assert not Path(old).exists()
        stop(second)
        stop(spectator)
        third, newest = start('third', ['--attach', 'discovery'])
        attached = request(newest, 'list')['panes'][0]
        request(newest, 'send', attached['id'], b'2\n')
        report = json.loads(wait_for_file(base / 'report-2.json', third))
        assert report['endpoint'] == newest and report['inherited'] == old
        assert report['pid'] == original['pid']
        # An unrelated process cannot silently adopt another pane's endpoint.
        outsider = subprocess.run([str(ROOT / 'battyctl'), 'list'],
                                  env=env | {'BATTY_CONTROL': newest}, capture_output=True, text=True, timeout=5)
        assert outsider.returncode != 0 and 'controlling pane' in outsider.stderr
        explicit = subprocess.run([str(ROOT / 'battyctl'), '--socket', newest, 'list'],
                                  env=env, capture_output=True, text=True, timeout=5)
        assert explicit.returncode == 0, explicit.stderr
        # Unsafe existing registries are rejected without changing their modes.
        unsafe = base / 'unsafe'
        unsafe.mkdir(mode=0o755); unsafe.chmod(0o755)
        check = subprocess.run([sys.executable, str(ROOT / 'tools/control_paths.py')],
                               env=env | {'BATTY_CONTROL_DIR': str(unsafe)}, capture_output=True, text=True)
        assert check.returncode != 0 and unsafe.stat().st_mode & 0o777 == 0o755
        stop(third)
        (stale / 'control.sock').unlink(); stale.rmdir()
        assert not list(registry.iterdir()), list(registry.iterdir())
    finally:
        for process in processes:
            if process.poll() is None:
                try: stop(process)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
        for log in logs:
            log.flush(); log.seek(0)
            content = log.read()
            if 'Traceback' in content: print(content, file=sys.stderr)
            log.close()
print('PASS persistent endpoint discovery: live old frontend, two replacements, unchanged child, explicit override and private registry')
