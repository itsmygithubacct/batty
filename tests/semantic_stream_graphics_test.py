#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Real Kitty and Sixel pixels survive bounded network compression and native viewing."""
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from kilix_frame_source import FrameSource
from kilix_semantic_wire import (COMPRESSED_MAGIC, COMPRESSED_SIZE, authenticate_client,
                                 exact, receive_header)
from kilix_stream_mirror import StreamMirror
from kilix_stream_server import StreamServer

root = Path(os.environ['BATTY_TEST_SESSION_DIR'])
mirror_root = root / 'mirror'
env = os.environ | {'BATTY_CONFIG': '/dev/null'}
script = ('import subprocess,time,sys; '
          'sys.stdout.buffer.write(subprocess.check_output([sys.executable, '
          '"tools/graphics-demo.py", "--once", "--protocol", "both", '
          '"--width", "480", "--height", "300"])); '
          'sys.stdout.buffer.flush(); time.sleep(20)')
owner = subprocess.Popen([str(ROOT / 'batty'), '--headless', '--session', 'graphics-stream',
                          '--session-dir', str(root), '--', 'python3', '-u', '-c', script],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
server = mirror = viewer = None
server_thread = mirror_thread = None
try:
    deadline = time.monotonic() + 10
    while not (root / 'graphics-stream.sock').exists():
        assert owner.poll() is None and time.monotonic() < deadline
        time.sleep(0.02)
    server = StreamServer(root, 'graphics-stream')
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    with socket.create_connection(('127.0.0.1', server.port), timeout=3) as remote:
        remote.settimeout(5)
        authenticate_client(remote, server.token)
        deadline = time.monotonic() + 9
        while True:
            assert time.monotonic() < deadline, 'Image-bearing network frame did not arrive'
            words = receive_header(remote)
            if not words[1]:
                continue
            if words[0] == COMPRESSED_MAGIC:
                encoded = COMPRESSED_SIZE.unpack(exact(remote, COMPRESSED_SIZE.size))[0]
                assert 0 < encoded < words[1]
                payload = zlib.decompress(exact(remote, encoded))
            else:
                encoded = words[1]
                payload = exact(remote, encoded)
            assert len(payload) == words[1] and payload[:8] == b'BTPRES01'
            if struct.unpack_from('<I', payload, 60)[0] >= 2:
                assert words[0] == COMPRESSED_MAGIC
                assert encoded * 2 < words[1], 'Graphical frame compression saved under 50%'
                break
    mirror = StreamMirror('127.0.0.1', server.port, server.token, mirror_root, 'graphics-relay')
    mirror_thread = threading.Thread(target=mirror.serve_forever, daemon=True)
    mirror_thread.start()
    mirror.ready()
    viewer = subprocess.Popen([str(ROOT / 'batty'), '--headless', '--observe', 'graphics-relay',
                               '--session-dir', str(mirror_root)],
                              cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with FrameSource(mirror_root, 'graphics-relay') as source:
        deadline = time.monotonic() + 5
        image_count = struct.unpack_from('<I', os.pread(source.frame.fd, 64, 0), 60)[0]
        while image_count < 2:
            assert time.monotonic() < deadline, 'Mirrored graphics did not reach native observer'
            frame = source.poll()
            if frame:
                with frame:
                    image_count = struct.unpack_from('<I', os.pread(frame.fd, 64, 0), 60)[0]
        assert viewer.poll() is None and owner.poll() is None
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
    subprocess.run([str(ROOT / 'batty'), '--terminate', 'graphics-stream', '--session-dir', str(root)],
                   cwd=ROOT, env=env, capture_output=True, timeout=8, check=True)

print('PASS compressed Kitty/Sixel frame, local mirror and native observer')
