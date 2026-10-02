#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A dropped frame socket reconnects without detaching a native viewer."""
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from kilix_frame_source import FrameSource
from kilix_stream_server import StreamServer
from kilix_stream_mirror import StreamMirror


def content(frame):
    header = os.pread(frame.fd, 76, 0)
    cells, count = struct.unpack_from('<II', header, 52)
    title = struct.unpack_from('<I', header, 68)[0]
    raw = os.pread(frame.fd, count * 4, 860 + title + cells * 31)
    return ''.join(chr(value) for value in struct.unpack('<' + 'I' * count, raw))


root = Path(os.environ['BATTY_TEST_SESSION_DIR'])
mirror_root = root / 'reconnect-mirror'
env = os.environ | {'BATTY_CONFIG': '/dev/null'}
owner = subprocess.Popen([str(ROOT / 'batty'), '--headless', '--session', 'reconnect',
                          '--session-dir', str(root), '--', 'python3', '-u', '-c',
                          'import time; print("RECONNECT_FIRST", flush=True); '
                          'time.sleep(2); print("RECONNECT_SECOND", flush=True); time.sleep(20)'],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
server = mirror = viewer = None
server_thread = mirror_thread = None
try:
    deadline = time.monotonic() + 10
    while not (root / 'reconnect.sock').exists():
        assert owner.poll() is None and time.monotonic() < deadline
        time.sleep(0.02)
    server = StreamServer(root, 'reconnect')
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    mirror = StreamMirror('127.0.0.1', server.port, server.token, mirror_root, 'relay')
    mirror_thread = threading.Thread(target=mirror.serve_forever, daemon=True)
    mirror_thread.start()
    mirror.ready()
    viewer = subprocess.Popen([str(ROOT / 'batty'), '--headless', '--observe', 'relay',
                               '--session-dir', str(mirror_root)],
                              cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with FrameSource(mirror_root, 'relay') as source:
        deadline = time.monotonic() + 2
        first = content(source.frame)
        while 'RECONNECT_FIRST' not in first:
            assert time.monotonic() < deadline
            frame = source.poll()
            if frame:
                with frame:
                    first = content(frame)
        with server.lock:
            assert len(server.clients) == 1
            network = next(iter(server.clients))
            network.shutdown(socket.SHUT_RDWR)
            deadline = time.monotonic() + 2
            while mirror.connected:
                assert time.monotonic() < deadline, 'Mirror did not notice the dropped frame socket'
                time.sleep(0.01)
            frame = source.poll(0)
            if frame:
                frame.close()
            assert source.latest_words[18] & (1 << 10), 'Native observer missed upstream disconnect state'
        deadline = time.monotonic() + 5
        while mirror.reconnects < 1:
            assert time.monotonic() < deadline, 'Frame stream did not reconnect'
            time.sleep(0.02)
        frame = source.poll(0)
        if frame:
            frame.close()
        assert not source.latest_words[18] & (1 << 10), 'Native observer retained stale disconnect state'
        assert viewer.poll() is None, 'Native observer detached during network reconnect'
        seen = first
        deadline = time.monotonic() + 5
        while 'RECONNECT_SECOND' not in seen:
            assert time.monotonic() < deadline, 'New owner output did not cross reconnected stream'
            frame = source.poll()
            if frame:
                with frame:
                    seen = content(frame)
        assert owner.poll() is None and viewer.poll() is None
finally:
    if viewer is not None:
        viewer.terminate()
        try:
            viewer.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            viewer.kill()
            viewer.communicate(timeout=3)
    if mirror is not None:
        mirror.close()
    if mirror_thread is not None:
        mirror_thread.join(timeout=3)
    if server is not None:
        server.close()
    if server_thread is not None:
        server_thread.join(timeout=3)
    owner.terminate()
    try:
        owner.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        owner.kill()
        owner.communicate(timeout=3)
    subprocess.run([str(ROOT / 'batty'), '--terminate', 'reconnect', '--session-dir', str(root)],
                   cwd=ROOT, env=env, capture_output=True, timeout=8, check=True)

assert not (mirror_root / 'relay.sock').exists()
print('PASS dropped frame socket reconnects with native observer and exact owner')
