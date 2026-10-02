#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Remote semantic frames through a mirror into a real Batty observer."""
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from kilix_frame_source import FrameSource
from kilix_input_source import InputSource
from kilix_semantic_wire import PATCH_HEADER, PATCH_MAGIC, PATCH_RANGE
from kilix_stream_mirror import StreamMirror


def content(frame):
    header = os.pread(frame.fd, 76, 0)
    cells, count = struct.unpack_from('<II', header, 52)
    title = struct.unpack_from('<I', header, 68)[0]
    raw = os.pread(frame.fd, count * 4, 860 + title + cells * 31)
    return ''.join(chr(value) for value in struct.unpack('<' + 'I' * count, raw))


root = Path(os.environ['BATTY_TEST_SESSION_DIR'])
mirror_root = root / 'mirror'
env = os.environ | {'BATTY_CONFIG': '/dev/null'}
owner = subprocess.Popen([str(ROOT / 'batty'), '--headless', '--session', 'source',
                          '--session-dir', str(root), '--', 'python3', '-u', '-c',
                          'import sys,time; sys.stdout.write("MIRROR_ONE"); sys.stdout.flush(); '
                          'time.sleep(1.5); sys.stdout.write("\\rMIRROR_TWO"); '
                          'sys.stdout.flush(); time.sleep(20)'],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
server = viewer = None
try:
    deadline = time.monotonic() + 10
    while not (root / 'source.sock').exists():
        assert owner.poll() is None and time.monotonic() < deadline
        time.sleep(0.02)
    server = subprocess.Popen([str(ROOT / 'kilix'), 'remote', 'serve', '--session', 'source',
                               '--session-dir', str(root)],
                              cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    line = server.stderr.readline().decode('ascii', 'replace').strip().split()
    assert len(line) == 5 and line[:2] == ['kilix', 'remote:'] and line[3] == 'token', 'Remote serve did not start'
    address, token = line[2], line[4]
    second = server.stderr.readline().decode('ascii', 'replace').strip().split()
    third = server.stderr.readline().decode('ascii', 'replace').strip().split()
    assert (address.startswith('127.0.0.1:') and len(token) == 32 and
            second[:3] == ['kilix', 'remote:', 'input-port'] and
            third[:3] == ['kilix', 'remote:', 'view-token'] and len(third[3]) == 32)
    viewer = subprocess.Popen([str(ROOT / 'kilix'), 'remote', 'view', '--headless',
                               '--port', address.rsplit(':', 1)[1],
                               '--input-port', second[3], '--observe', '--token', third[3],
                               '--session-dir', str(mirror_root)],
                              cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    mirror_name = f'remote-{viewer.pid}'
    deadline = time.monotonic() + 5
    while not (mirror_root / f'{mirror_name}.sock').exists():
        assert viewer.poll() is None and time.monotonic() < deadline, 'Remote view did not start'
        time.sleep(0.02)
    deadline = time.monotonic() + 5
    while True:
        try:
            source = FrameSource(mirror_root, mirror_name)
            break
        except (ConnectionError, OSError):
            assert viewer.poll() is None and time.monotonic() < deadline, 'Remote mirror did not become ready'
            time.sleep(0.02)
    with source:
        seen = content(source.frame)
        seen_one = 'MIRROR_ONE' in seen
        deadline = time.monotonic() + 7
        while 'MIRROR_TWO' not in seen:
            assert time.monotonic() < deadline, 'Remote mirror did not deliver the updated frame'
            frame = source.poll()
            if frame:
                with frame:
                    seen = content(frame)
                    seen_one |= 'MIRROR_ONE' in seen
        assert seen_one and owner.poll() is None
        assert viewer.poll() is None, 'Native observer rejected remote semantic frames'
        with InputSource(mirror_root, mirror_name, read_only=True) as text_source:
            assert b'MIRROR_TWO' in text_source.text(False), 'Read-only CLI viewer cannot query pane text'
        malformed = StreamMirror.__new__(StreamMirror)
        malformed.frame_size = source.frame.size
        values = (PATCH_MAGIC, source.frame.size, source.frame.epoch,
                  source.frame.revision + 1, source.frame.columns, source.frame.rows,
                  source.frame.cell_width, source.frame.cell_height)
        receiver, sender = socket.socketpair()
        with receiver, sender:
            sender.sendall(PATCH_HEADER.pack(source.frame.revision, 1) +
                           PATCH_RANGE.pack(source.frame.size, 1))
            try:
                malformed._receive_patch(receiver, values, source.frame.fd,
                                         source.frame.revision)
            except ValueError as error:
                assert 'patch range' in str(error)
            else:
                raise AssertionError('Out-of-bounds semantic patch was accepted')
finally:
    if viewer is not None:
        viewer.terminate()
        try:
            viewer.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            viewer.kill()
            viewer.communicate(timeout=3)
    if server is not None:
        server.terminate()
        try:
            server.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate(timeout=3)
    owner.terminate()
    try:
        owner.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        owner.kill()
        owner.communicate(timeout=3)
    subprocess.run([str(ROOT / 'batty'), '--terminate', 'source', '--session-dir', str(root)],
                   cwd=ROOT, env=env, capture_output=True, timeout=8, check=True)

assert not (mirror_root / f'{mirror_name}.sock').exists()
print('PASS network semantic mirror, live updates, native observer and owner isolation')
