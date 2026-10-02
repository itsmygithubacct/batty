#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A read-only semantic frame source attached beside a live Batty frontend."""
import os
from pathlib import Path
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from kilix_frame_source import FrameSource


def codepoints(frame):
    header = os.pread(frame.fd, 76, 0)
    assert header[:8] == b'BTPRES01'
    cells, count = struct.unpack_from('<II', header, 52)
    title = struct.unpack_from('<I', header, 68)[0]
    assert cells == frame.columns * frame.rows and count <= 4 * 1024 * 1024
    raw = os.pread(frame.fd, count * 4, 860 + title + cells * 31)
    assert len(raw) == count * 4
    return ''.join(chr(value) for value in struct.unpack('<' + 'I' * count, raw))


session_root = Path(os.environ['BATTY_TEST_SESSION_DIR'])
command = ['python3', '-u', '-c',
           'import time; print("SEMANTIC_ONE", flush=True); '
           'time.sleep(0.8); print("SEMANTIC_TWO", flush=True); time.sleep(20)']
env = os.environ | {'BATTY_CONFIG': '/dev/null'}
owner = subprocess.Popen([str(ROOT / 'batty'), '--headless', '--session', 'frame',
                          '--session-dir', str(session_root), '--', *command],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
try:
    deadline = time.monotonic() + 10
    while not (session_root / 'frame.sock').exists():
        assert owner.poll() is None and time.monotonic() < deadline, 'Persistent owner did not start'
        time.sleep(0.02)
    with FrameSource(session_root, 'frame') as source:
        first_revision = source.revision
        seen = codepoints(source.frame)
        deadline = time.monotonic() + 5
        while 'SEMANTIC_ONE' not in seen:
            assert time.monotonic() < deadline, 'First semantic frame never arrived'
            frame = source.poll()
            if frame:
                with frame:
                    seen = codepoints(frame)
        assert source.epoch and source.revision >= first_revision
        deadline = time.monotonic() + 5
        while 'SEMANTIC_TWO' not in seen:
            assert time.monotonic() < deadline, 'Changed semantic frame never arrived'
            frame = source.poll()
            if frame:
                with frame:
                    seen = codepoints(frame)
        assert owner.poll() is None, 'Observer displaced the owning frontend'
    assert owner.poll() is None, 'Closing observer ended the owner'
finally:
    owner.terminate()
    try:
        owner.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        owner.kill()
        owner.communicate(timeout=3)
    subprocess.run([str(ROOT / 'batty'), '--terminate', 'frame', '--session-dir', str(session_root)],
                   cwd=ROOT, env=env, capture_output=True, timeout=8, check=True)

print('PASS sealed full semantic frames, live revisions and read-only observer lifetime')
