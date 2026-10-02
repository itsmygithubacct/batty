#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Real frontend-owned maintenance, input isolation and surviving host takeover."""
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from control import request
from kilix_panes import child_pids
import kilix_transcript as api
import transcript_storage as storage


def wait_for(check):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError('Automatic maintenance deadline')


def record(root, name):
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    parent.settimeout(5)
    process = subprocess.Popen([str(ROOT / 'build/batty-transcript'), str(root), name, '32768', 'keep'], stdin=child)
    child.close(); parent.sendall(b'Dbackground retention'); parent.sendall(b'E')
    assert struct.unpack('<I', parent.recv(4))[0] == 0
    parent.close(); assert process.wait(timeout=5) == 0


def watcher(process):
    children = child_pids(process.pid, {})
    for child in children:
        try:
            args = Path(f'/proc/{child}/cmdline').read_bytes().split(b'\0')
            if any(a.endswith(b'/transcript_maintenance.py') for a in args):
                return int(child)
        except FileNotFoundError:
            pass
    return None


with tempfile.TemporaryDirectory(prefix='bt-auto-recording-') as directory:
    root = Path(directory)
    logs = root / 'logs'; logs.mkdir(mode=0o700)
    settings = root / 'settings.conf'; settings.write_text('KILIX_TRANSCRIPT=on\n')
    env = os.environ | {'BATTY_TRANSCRIPT_DIR': str(logs), 'BATTY_TRANSCRIPT_MAINTENANCE': '1',
                        'GPU_TERMINAL_SETTINGS_FILE': str(settings), 'BATTY_CONTROL_DIR': str(root / 'control'),
                        'BATTY_KILIX_CONFIG': str(ROOT / 'tests/recording_config.bash'),
                        'BATTY_SESSION_DIR': str(root / 'sessions'),
                        'XDG_STATE_HOME': str(root / 'state'),
                        'BATTY_KILIX_AUTO_RECOVER': '0'}
    processes, descriptors = [], []
    def start(label):
        ready = root / (label + '.ready')
        log = (root / (label + '.out')).open('w')
        process = subprocess.Popen([str(ROOT / 'kilix'), '--', '/bin/cat'],
            env=env | {'BATTY_RECORDING_READY': str(ready)}, stdout=log, stderr=log)
        processes.append((process, log))
        endpoint = wait_for(lambda: ready.read_text().strip() if ready.exists() else None)
        pane = request(endpoint, 'list')['panes'][0]
        pid = wait_for(lambda: watcher(process))
        descriptor = os.pidfd_open(pid); descriptors.append(descriptor)
        return process, endpoint, pane, descriptor
    try:
        record(logs, 'before-first-window')
        first, endpoint, pane, first_watch = start('first')
        wait_for(lambda: (logs / 'recent/before-first-window.log.zst').exists()
                 and not (logs / 'before-first-window.log').exists())
        assert not (logs / 'before-first-window.log').exists()
        second, second_endpoint, second_pane, second_watch = start('second')
        fd = api.open_root(str(logs))
        try:
            # Maintenance exclusion is entirely outside the terminal pump.
            with storage.maintenance_lock(fd, True):
                request(endpoint, 'send', pane['id'], b'INPUT_DURING_MAINTENANCE\n')
                wait_for(lambda: 'INPUT_DURING_MAINTENANCE' in request(endpoint, 'dump', pane['id']))
                assert request(endpoint, 'ping')['version'] == 1
        finally:
            os.close(fd)
        record(logs, 'after-first-window')
        request(endpoint, 'close', pane['id']); first.wait(timeout=5)
        assert select.select([first_watch], [], [], 5)[0], 'First watcher did not exit with its frontend'
        # The next frontend acquires leadership within its one-second retry.
        wait_for(lambda: (logs / 'recent/after-first-window.log.zst').exists()
                 and not (logs / 'after-first-window.log').exists())
        assert not (logs / 'after-first-window.log').exists()
        assert not select.select([second_watch], [], [], 0)[0]
        request(second_endpoint, 'close', second_pane['id']); second.wait(timeout=5)
        assert select.select([second_watch], [], [], 5)[0], 'Second watcher did not exit'
    finally:
        for process, log in processes:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
            log.close()
        for fd in descriptors:
            os.close(fd)
print('PASS automatic maintenance: background compression, responsive input, host takeover and pidfd-observed cleanup')
