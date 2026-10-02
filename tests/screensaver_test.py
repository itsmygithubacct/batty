#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Compile the pinned screensaver privately and exercise its terminal lifecycle."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import tempfile
import termios
import time

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'third_party/kilix-desktop/src/config/screensavers/matrix.c'
MANIFEST = ROOT / 'third_party/kilix-desktop/upstream.json'

assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == json.loads(
    MANIFEST.read_text())['files']['src/config/screensavers/matrix.c']
with tempfile.TemporaryDirectory(prefix='bt-screensaver-') as directory:
    env = os.environ | {'BATTY_KILIX_STORAGE_HOME': str(Path(directory) / 'storage'),
                        'TERM': 'xterm-256color'}
    install = subprocess.run([str(ROOT / 'kilix'), 'screensaver', '--install-only'],
                             env=env, capture_output=True, text=True, timeout=30)
    assert install.returncode == 0, install.stderr
    binary = Path(install.stdout.strip())
    assert binary.is_file() and binary.stat().st_mode & 0o100
    assert str(binary).startswith(directory)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
    process = subprocess.Popen([str(ROOT / 'kilix'), 'screensaver', 'matrix'],
                               env=env, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    try:
        output = bytearray()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and b'\x1b[?1049h' not in output:
            if select.select([master], [], [], 0.1)[0]:
                output.extend(os.read(master, 65536))
        assert b'\x1b[?1049h' in output, 'Screensaver did not enter alternate screen'
        os.write(master, b'q')
        assert process.wait(timeout=3) == 0
        while select.select([master], [], [], 0)[0]:
            try:
                output.extend(os.read(master, 65536))
            except OSError:
                break
        assert b'\x1b[?1049l' in output, 'Screensaver did not restore terminal'
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        os.close(master)
    assert not list(Path(directory).rglob('.matrix-*'))

print('PASS pinned matrix source, private build, alternate-screen run and q exit')
