#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Bounded maintenance passes and cancellation of an in-flight codec."""
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import kilix_transcript as api
import transcript_storage as storage


def record(root, name):
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    parent.settimeout(5)
    process = subprocess.Popen([str(ROOT / 'build/batty-transcript'), str(root), name, '32768', 'keep'], stdin=child)
    child.close()
    parent.sendall(b'Dretained output'); parent.sendall(b'E')
    assert struct.unpack('<I', parent.recv(4))[0] == 0
    parent.close(); assert process.wait(timeout=5) == 0


with tempfile.TemporaryDirectory(prefix='bt-maintenance-') as directory:
    root = Path(directory)
    for i in range(3):
        record(root, f'bounded-{i}')
    fd = api.open_root(str(root))
    try:
        with storage.maintenance_lock(fd, True):
            first = storage.maintain(fd, api, 100000, 100000, max_operations=1)
            assert first['operations'] == 1 and first['compressed'] == 1 and first['deferred'], first
            assert len(list(root.glob('*.log'))) == 2
            second = storage.maintain(fd, api, 0, 0, max_operations=1)
            assert second['operations'] == 1 and second['removed'] == 1 and second['compressed'] == 0, second
            # Repeated limited passes converge without starving budget eviction.
            for _ in range(4):
                result = storage.maintain(fd, api, 0, 0, max_operations=1)
                assert result['operations'] <= 1
            assert not api.index(fd)
        # A stalled codec is owned and reaped when the frontend cancellation
        # predicate fires; no global PID signalling or shell is involved.
        codec = root / 'zstd'
        pidfile = root / 'codec.pid'
        codec.write_text('#!' + sys.executable + '\nimport os, time\n'
                         + f'open({str(pidfile)!r}, "w").write(str(os.getpid()))\ntime.sleep(60)\n')
        codec.chmod(0o700)
        with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as output:
            begin = time.monotonic()
            storage.cancelled = lambda: time.monotonic() - begin >= 0.5
            try:
                with patch.dict(os.environ, {'PATH': str(root)}):
                    try: storage.run_codec(source.fileno(), output.fileno())
                    except InterruptedError: pass
                    else: raise AssertionError('Expected owner cancellation')
            finally:
                storage.cancelled = lambda: False
            assert time.monotonic() - begin < 3
            pid = int(pidfile.read_text())
            try: os.kill(pid, 0)
            except ProcessLookupError: pass
            else: raise AssertionError('Cancelled codec was not reaped')
    finally:
        os.close(fd)

with tempfile.TemporaryDirectory(prefix='bt-maintenance-timer-') as directory:
    root = Path(directory)
    settings = root / 'settings.conf'; settings.write_text('KILIX_TRANSCRIPT_MAX_TOTAL=1G\n')
    process = subprocess.Popen([sys.executable, str(ROOT / 'tools/transcript_maintenance.py'), str(os.getpid()),
                                '--interval', '0.1', '--operations', '1'],
        env=os.environ | {'BATTY_TRANSCRIPT_DIR': str(root), 'GPU_TERMINAL_SETTINGS_FILE': str(settings)},
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        preexec_fn=lambda: os.umask(0o777))
    try:
        for name in ('first-tick', 'later-tick'):
            record(root, name)
            deadline = time.monotonic() + 5
            while not (root / 'recent' / (name + '.log.zst')).exists() or (root / (name + '.log')).exists():
                assert process.poll() is None
                assert time.monotonic() < deadline
                time.sleep(0.02)
            assert not (root / (name + '.log')).exists()
        assert stat.S_IMODE((root / '.watch.lock').stat().st_mode) == 0o600
        assert stat.S_IMODE((root / '.maintenance.lock').stat().st_mode) == 0o600
        assert stat.S_IMODE((root / 'recent').stat().st_mode) == 0o700
    finally:
        process.terminate(); process.communicate(timeout=5)
print('PASS bounded maintenance: pass limits, eviction priority, convergence and codec cancellation/reaping')
