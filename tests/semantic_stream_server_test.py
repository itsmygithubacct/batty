#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Authenticated network transfer of Batty's live semantic presentation."""
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
from kilix_semantic_wire import (AUTH_REQUEST, AUTH_REJECT, COMPRESSED_MAGIC, COMPRESSED_SIZE,
                                 LEGACY_FRAME_HEADER, LEGACY_FRAME_MAGIC,
                                 PATCH_HEADER, PATCH_MAGIC, PATCH_RANGE,
                                 authenticate_client, exact, receive_header)
from kilix_stream_server import StreamServer


def text(frame):
    assert frame[:8] == b'BTPRES01'
    cells, count = struct.unpack_from('<II', frame, 52)
    title = struct.unpack_from('<I', frame, 68)[0]
    start = 860 + title + cells * 31
    chars = struct.unpack_from('<' + 'I' * count, frame, start)
    return ''.join(chr(value) for value in chars)


root = Path(os.environ['BATTY_TEST_SESSION_DIR'])
legacy_writer, legacy_reader = socket.socketpair()
try:
    legacy_writer.sendall(LEGACY_FRAME_HEADER.pack(
        LEGACY_FRAME_MAGIC, 860, 1, 1, 1, 1, 10, 20, 1, 0, 0, 0, 0, 0))
    assert receive_header(legacy_reader)[-1] == 0
finally:
    legacy_writer.close()
    legacy_reader.close()
env = os.environ | {'BATTY_CONFIG': '/dev/null'}
owner = subprocess.Popen([str(ROOT / 'batty'), '--headless', '--session', 'stream',
                          '--session-dir', str(root), '--', 'python3', '-u', '-c',
                          'import sys,time; sys.stdout.write("NETWORK_ONE"); sys.stdout.flush(); '
                          'time.sleep(2); sys.stdout.write("\\rNETWORK_TWO"); '
                          'sys.stdout.flush(); time.sleep(20)'],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
server = None
thread = None
try:
    deadline = time.monotonic() + 10
    while not (root / 'stream.sock').exists():
        assert owner.poll() is None and time.monotonic() < deadline
        time.sleep(0.02)
    server = StreamServer(root, 'stream')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with socket.create_connection(('127.0.0.1', server.port), timeout=3) as denied:
        denied.settimeout(3)
        denied.sendall(AUTH_REQUEST + b'\0' * 16)
        assert exact(denied, 4) == AUTH_REJECT
    with socket.create_connection(('127.0.0.1', server.port), timeout=3) as remote:
        remote.settimeout(5)
        authenticate_client(remote, server.token)
        seen = ''
        seen_one = False
        revisions = set()
        compressed = 0
        patches = 0
        patch_wire_bytes = 0
        previous = None
        previous_revision = 0
        sizes = []
        deadline = time.monotonic() + 6
        while 'NETWORK_TWO' not in seen:
            assert time.monotonic() < deadline, 'Live semantic network revision did not arrive'
            header = receive_header(remote)
            size, epoch, revision, cols, rows = header[1:6]
            sizes.append((size, header[0]))
            assert epoch and cols and rows
            if size:
                if header[0] == PATCH_MAGIC:
                    base_revision, count = PATCH_HEADER.unpack(exact(remote, PATCH_HEADER.size))
                    assert previous is not None and base_revision == previous_revision
                    assert len(previous) == size and 0 < count <= 1024
                    patched = bytearray(previous)
                    end = 0
                    patch_size = PATCH_HEADER.size
                    for _ in range(count):
                        start, length = PATCH_RANGE.unpack(exact(remote, PATCH_RANGE.size))
                        assert start >= end and 0 < length <= size - start
                        patched[start:start + length] = exact(remote, length)
                        patch_size += PATCH_RANGE.size + length
                        end = start + length
                    payload = bytes(patched)
                    assert patch_size * 4 < size, 'Patch was not materially smaller than a full frame'
                    patch_wire_bytes += patch_size
                    patches += 1
                elif header[0] == COMPRESSED_MAGIC:
                    encoded_size = COMPRESSED_SIZE.unpack(exact(remote, COMPRESSED_SIZE.size))[0]
                    assert 0 < encoded_size < size
                    payload = zlib.decompress(exact(remote, encoded_size))
                    compressed += 1
                else:
                    payload = exact(remote, size)
                assert len(payload) == size
                assert payload[:8] == b'BTPRES01'
                assert struct.unpack_from('<IIQQ', payload, 8) == (1, size, epoch, revision)
                seen = text(payload)
                seen_one |= 'NETWORK_ONE' in seen
                revisions.add(revision)
                previous, previous_revision = payload, revision
        assert seen_one and len(revisions) >= 2 and compressed and patches, (seen, revisions, compressed, patches, sizes)
        assert owner.poll() is None, 'Network observer displaced the PTY owner'
finally:
    if server is not None:
        server.close()
    if thread is not None:
        thread.join(timeout=3)
    owner.terminate()
    try:
        owner.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        owner.kill()
        owner.communicate(timeout=3)
    subprocess.run([str(ROOT / 'batty'), '--terminate', 'stream', '--session-dir', str(root)],
                   cwd=ROOT, env=env, capture_output=True, timeout=8, check=True)

print(f'PASS authenticated full/patch semantic revisions, {patch_wire_bytes} patch bytes, surviving PTY owner')
