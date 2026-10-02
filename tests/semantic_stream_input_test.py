#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Interactive input crosses the semantic bridge to the original PTY owner."""
from array import array
import errno
import fcntl
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
from kilix_frame_source import FrameSource, HEADER, HELLO, SEALS, packet, receive
from kilix_input_source import CLIPBOARD, INTENT, INTENT_LAYOUT, RESIZE, SEND, TEXT
from kilix_stream_server import StreamServer
from kilix_stream_mirror import StreamMirror
from kilix_semantic_wire import (AUTH_REJECT, AUTH_REQUEST, InputRejected,
                                 authenticate_client, exact, send_input)


def content(frame):
    header = os.pread(frame.fd, 76, 0)
    cells, count = struct.unpack_from('<II', header, 52)
    title = struct.unpack_from('<I', header, 68)[0]
    raw = os.pread(frame.fd, count * 4, 860 + title + cells * 31)
    return ''.join(chr(value) for value in struct.unpack('<' + 'I' * count, raw))


def operation(connection, number, kind, payload=b'', intent=b'', geometry=None, flags=0):
    words = list(HEADER.unpack(packet(number, kind, flags=flags)))
    words[6] = len(payload)
    if intent:
        words[21] = len(intent)
    if geometry:
        words[12:16] = geometry
    descriptor = -1
    try:
        if payload:
            descriptor = os.memfd_create('batty-test-input', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            os.write(descriptor, payload)
            fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, SEALS)
        ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                      array('i', [descriptor]).tobytes())] if descriptor >= 0 else []
        connection.sendmsg([HEADER.pack(*words) + intent], ancillary)
        response, returned = receive(connection, number, kind)
        assert response[11] == 0
        if kind == TEXT:
            if returned < 0:
                assert response[6] == 0
                return b''
            try:
                assert fcntl.fcntl(returned, fcntl.F_GET_SEALS) & SEALS == SEALS
                data = os.pread(returned, response[6], 0)
                assert len(data) == response[6]
                return data
            finally:
                os.close(returned)
        assert returned < 0
        return response
    finally:
        if descriptor >= 0:
            os.close(descriptor)


root = Path(os.environ['BATTY_TEST_SESSION_DIR'])
mirror_root = root / 'interactive-mirror'
env = os.environ | {'BATTY_CONFIG': '/dev/null'}
owner = subprocess.Popen([str(ROOT / 'batty'), '--headless', '--session', 'interactive',
                          '--session-dir', str(root), '--', '/bin/cat'],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
server = mirror = controller = None
server_thread = mirror_thread = None
try:
    deadline = time.monotonic() + 10
    while not (root / 'interactive.sock').exists():
        assert owner.poll() is None and time.monotonic() < deadline
        time.sleep(0.02)
    server = StreamServer(root, 'interactive')
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    with socket.create_connection(('127.0.0.1', server.input_port), timeout=3) as denied:
        denied.settimeout(3)
        denied.sendall(AUTH_REQUEST + b'\0' * 16)
        assert exact(denied, 4) == AUTH_REJECT
    mirror = StreamMirror('127.0.0.1', server.port, server.token, mirror_root, 'remote',
                          input_port=server.input_port)
    mirror_thread = threading.Thread(target=mirror.serve_forever, daemon=True)
    mirror_thread.start()
    mirror.ready()
    mirror.connect_input()
    with socket.create_connection(('127.0.0.1', server.input_port), timeout=3) as limited:
        limited.settimeout(5)
        authenticate_client(limited, server.view_token)
        assert exact(limited, 4) == b'BTIR', 'Read-only text peer did not attach beside controller'
        assert isinstance(send_input(limited, 1, TEXT, b'\0'), bytes)
        denied = ((SEND, b'READ_ONLY_REJECTED\n'),
                  (INTENT, INTENT_LAYOUT.pack(2, *([0] * 11))),
                  (RESIZE, struct.pack('!4I', 80, 24, 8, 16)),
                  (CLIPBOARD, b''))
        for number, (kind, body) in enumerate(denied, 2):
            try:
                send_input(limited, number, kind, body)
            except InputRejected as error:
                assert error.errno == errno.EPERM, (kind, error)
            else:
                raise AssertionError(f'Read-only network credential accepted input kind {kind}')
    controller = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    controller.settimeout(5)
    controller.connect(str(mirror_root / 'remote.sock'))
    controller.sendall(packet(1, HELLO, flags=1))
    words, descriptor = receive(controller, 1, HELLO)
    assert descriptor >= 0 and words[2] == mirror.metadata[2] and words[18] & 16 and not words[18] & 8
    os.close(descriptor)
    time.sleep(3.2)  # The authenticated input channel must survive idle periods.
    with FrameSource(mirror_root, 'remote') as observer:
        operation(controller, 2, SEND, b'NETWORK_SEND\n')
        operation(controller, 3, INTENT, b'NETWORK_PASTE\n',
                  INTENT_LAYOUT.pack(2, *([0] * 11)))
        operation(controller, 4, INTENT, intent=INTENT_LAYOUT.pack(3, 0, 0, 0, 0, 0, 1, 1, 10, 10, 0, 0))
        operation(controller, 5, INTENT, intent=INTENT_LAYOUT.pack(3, 1, 0, 0, 0, 0, 1, 0, 10, 10, 0, 0))
        operation(controller, 6, RESIZE, geometry=(91, 27, words[14], words[15]))
        operation(controller, 7, INTENT, intent=INTENT_LAYOUT.pack(5, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1))
        operation(controller, 8, INTENT, intent=INTENT_LAYOUT.pack(7, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1))
        seen = ''
        deadline = time.monotonic() + 7
        while not ('NETWORK_SEND' in seen and 'NETWORK_PASTE' in seen and
                   observer.latest_words[12:14] == (91, 27)):
            assert time.monotonic() < deadline, 'Interactive remote owner did not publish input'
            frame = observer.poll()
            if frame:
                with frame:
                    seen = content(frame)
        width = words[14]
        operation(controller, 9, INTENT, intent=INTENT_LAYOUT.pack(3, 0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0))
        operation(controller, 10, INTENT, intent=INTENT_LAYOUT.pack(3, 2, 0, 0, 0, 0, 0, 1, 14 * width, 0, 0, 0))
        operation(controller, 11, INTENT, intent=INTENT_LAYOUT.pack(3, 1, 0, 0, 0, 0, 1, 0, 14 * width, 0, 0, 0))
        selected = operation(controller, 12, TEXT, flags=1)
        assert b'NETWORK_SEND' in selected, 'Selected text did not cross the remote bridge'
        copied = operation(controller, 13, TEXT)
        assert b'NETWORK_SEND' in copied and b'NETWORK_PASTE' in copied
        with server.lock:
            input_connection = next(connection for connection in server.clients
                                    if connection.getsockname()[1] == server.input_port)
        input_connection.shutdown(socket.SHUT_RDWR)
        try:
            operation(controller, 14, SEND, b'INPUT_ACK_LOST\n')
        except OSError as error:
            assert error.errno == errno.EIO
        else:
            raise AssertionError('Dropped input acknowledgment was reported as accepted')
        recovered = None
        for number in range(15, 23):
            payload = f'INPUT_RECOVERED_{number}\n'.encode()
            try:
                operation(controller, number, SEND, payload)
            except OSError as error:
                assert error.errno in (errno.ENOTCONN, errno.EIO)
                time.sleep(0.1)
            else:
                recovered = payload.decode().strip()
                break
        assert recovered, 'Later input did not reconnect'
        deadline = time.monotonic() + 5
        while recovered not in seen:
            assert time.monotonic() < deadline, 'Accepted input did not reach the original owner'
            frame = observer.poll()
            if frame:
                with frame:
                    seen = content(frame)
        assert owner.poll() is None
finally:
    if controller is not None:
        controller.close()
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
    subprocess.run([str(ROOT / 'batty'), '--terminate', 'interactive', '--session-dir', str(root)],
                   cwd=ROOT, env=env, capture_output=True, timeout=8, check=True)

assert not (mirror_root / 'remote.sock').exists()
print('PASS interactive semantic network input, text copy, pointer, resize and reconnect')
