#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""An input peer reaches the owner while its local controller remains active."""
import errno
import os
from pathlib import Path
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from kilix_frame_source import FrameSource, packet, receive
from kilix_input_source import InputSource, SEND


def content(frame):
    header = os.pread(frame.fd, 76, 0)
    cells, count = struct.unpack_from('<II', header, 52)
    title = struct.unpack_from('<I', header, 68)[0]
    raw = os.pread(frame.fd, count * 4, 860 + title + cells * 31)
    return ''.join(chr(value) for value in struct.unpack('<' + 'I' * count, raw))


root = Path(os.environ['BATTY_TEST_SESSION_DIR'])
env = os.environ | {'BATTY_CONFIG': '/dev/null'}
owner = subprocess.Popen([str(ROOT / 'batty'), '--headless', '--session', 'input',
                          '--session-dir', str(root), '--', '/bin/cat'],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
try:
    deadline = time.monotonic() + 10
    while not (root / 'input.sock').exists():
        assert owner.poll() is None and time.monotonic() < deadline
        time.sleep(0.02)
    with FrameSource(root, 'input') as observer, InputSource(root, 'input') as writer:
        assert owner.poll() is None, 'Input peer displaced the local controller'
        try:
            InputSource(root, 'input')
        except OSError as error:
            assert error.errno == errno.EBUSY
        else:
            raise AssertionError('A second input peer was accepted')
        observer.number += 1
        observer.sock.sendall(packet(observer.number, SEND))
        try:
            receive(observer.sock, observer.number, SEND)
        except OSError as error:
            assert error.errno == errno.EPERM
        else:
            raise AssertionError('Read-only observer gained input permission')
        writer.send(b'REMOTE_SEND\n')
        writer.intent((2,) + (0,) * 11, b'REMOTE_PASTE\n')
        writer.resize(88, 26, writer.frame.cell_width, writer.frame.cell_height)
        seen = ''
        deadline = time.monotonic() + 7
        while not ('REMOTE_SEND' in seen and 'REMOTE_PASTE' in seen and
                   observer.latest_words[12:14] == (88, 26)):
            assert time.monotonic() < deadline, 'Owner did not publish interactive input and resize'
            frame = observer.poll()
            if frame:
                with frame:
                    seen = content(frame)
        assert owner.poll() is None
finally:
    owner.terminate()
    try:
        owner.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        owner.kill()
        owner.communicate(timeout=3)
    subprocess.run([str(ROOT / 'batty'), '--terminate', 'input', '--session-dir', str(root)],
                   cwd=ROOT, env=env, capture_output=True, timeout=8, check=True)

print('PASS independent input peer: send, paste, resize, limits and active local controller')
