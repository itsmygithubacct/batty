#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Terminal OSC 52 clipboard output reaches only the active remote controller."""
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
from kilix_input_source import CLIPBOARD, INTENT, INTENT_LAYOUT, SEND
from kilix_stream_server import StreamServer
from kilix_stream_mirror import StreamMirror


def operation(connection, number, kind, payload=b'', intent=b''):
    words = list(HEADER.unpack(packet(number, kind)))
    words[6] = len(payload)
    words[21] = len(intent)
    descriptor = -1
    try:
        if payload:
            descriptor = os.memfd_create('batty-clipboard-input', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            os.write(descriptor, payload)
            fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, SEALS)
        ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                      array('i', [descriptor]).tobytes())] if descriptor >= 0 else []
        connection.sendmsg([HEADER.pack(*words) + intent], ancillary)
        reply, received = receive(connection, number, kind)
        assert reply[11] == 0
        if kind != CLIPBOARD:
            assert received < 0
            return b''
        assert received >= 0 and reply[6] <= 1024 * 1024 + 1
        try:
            assert fcntl.fcntl(received, fcntl.F_GET_SEALS) & SEALS == SEALS
            result = os.pread(received, reply[6], 0)
            assert len(result) == reply[6]
            return result
        finally:
            os.close(received)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


root = Path(os.environ['BATTY_TEST_SESSION_DIR'])
mirror_root = root / 'clipboard-mirror'
env = os.environ | {'BATTY_CONFIG': '/dev/null'}
script = ('import sys\n'
          'print("CLIP_READY", flush=True)\n'
          'for line in sys.stdin:\n'
          '    if line.strip() == "WRITE":\n'
          '        sys.stdout.write("\\x1b]52;c;UkVNT1RFX0NMSVA=\\x07")\n'
          '        sys.stdout.flush()\n'
          '    elif line.strip().startswith("AGAIN_"):\n'
          '        sys.stdout.write("\\x1b]52;c;U0VDT05EX0NMSVA=\\x07")\n'
          '        sys.stdout.flush()\n'
          '    elif line.strip() == "DISABLED":\n'
          '        sys.stdout.write("\\x1b]52;c;UkVNT1RFX0NMSVA=\\x07")\n'
          '        sys.stdout.flush()\n')
owner = subprocess.Popen([str(ROOT / 'batty'), '--headless', '--session', 'clipboard',
                          '--session-dir', str(root), '--', 'python3', '-u', '-c', script],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
server = mirror = controller = None
server_thread = mirror_thread = None
try:
    deadline = time.monotonic() + 10
    while not (root / 'clipboard.sock').exists():
        assert owner.poll() is None and time.monotonic() < deadline
        time.sleep(0.02)
    server = StreamServer(root, 'clipboard')
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    mirror = StreamMirror('127.0.0.1', server.port, server.token, mirror_root, 'relay',
                          input_port=server.input_port)
    mirror_thread = threading.Thread(target=mirror.serve_forever, daemon=True)
    mirror_thread.start()
    mirror.ready()
    mirror.connect_input()
    controller = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    controller.settimeout(5)
    controller.connect(str(mirror_root / 'relay.sock'))
    controller.sendall(packet(1, HELLO, flags=1))
    hello, frame = receive(controller, 1, HELLO)
    assert frame >= 0 and hello[18] & 16
    os.close(frame)
    with FrameSource(mirror_root, 'relay') as observer:
        policy = INTENT_LAYOUT.pack(7, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1)
        operation(controller, 2, INTENT, intent=policy)
        operation(controller, 3, SEND, b'WRITE\n')
        deadline = time.monotonic() + 5
        while not observer.latest_words[18] & 8:
            assert time.monotonic() < deadline, 'Owner OSC 52 did not reach the remote mirror'
            published = observer.poll()
            if published:
                published.close()
        observer.number += 1
        observer.sock.sendall(packet(observer.number, CLIPBOARD))
        try:
            receive(observer.sock, observer.number, CLIPBOARD)
        except OSError as error:
            assert error.errno == errno.EPERM
        else:
            raise AssertionError('Read-only observer fetched remote clipboard')
        copied = operation(controller, 4, CLIPBOARD)
        assert copied == b'REMOTE_CLIP\0'
        deadline = time.monotonic() + 5
        while observer.latest_words[18] & 8:
            assert time.monotonic() < deadline
            published = observer.poll()
            if published:
                published.close()
        with server.lock:
            input_connection = next(connection for connection in server.clients
                                    if connection.getsockname()[1] == server.input_port)
        input_connection.shutdown(socket.SHUT_RDWR)
        try:
            operation(controller, 5, SEND, b'LOST_ACK\n')
        except OSError as error:
            assert error.errno == errno.EIO
        else:
            raise AssertionError('Dropped clipboard input channel reported acceptance')
        recovered = None
        for number in range(6, 14):
            try:
                operation(controller, number, SEND, f'AGAIN_{number}\n'.encode())
            except OSError as error:
                assert error.errno in (errno.ENOTCONN, errno.EIO)
                time.sleep(0.1)
            else:
                recovered = number
                break
        assert recovered, 'Remote input did not reconnect for the next OSC 52 write'
        deadline = time.monotonic() + 5
        while not observer.latest_words[18] & 8:
            assert time.monotonic() < deadline, 'Clipboard policy was not restored after reconnect'
            published = observer.poll()
            if published:
                published.close()
        copied = operation(controller, recovered + 1, CLIPBOARD)
        assert copied == b'SECOND_CLIP\0'
        disabled = INTENT_LAYOUT.pack(7, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        operation(controller, recovered + 2, INTENT, intent=disabled)
        deadline = time.monotonic() + 5
        while observer.latest_words[18] & 8:
            assert time.monotonic() < deadline, 'Clipboard pending state survived consumption'
            published = observer.poll()
            if published:
                published.close()
        before = observer.latest_words[3]
        operation(controller, recovered + 3, SEND, b'DISABLED\n')
        deadline = time.monotonic() + 5
        while observer.latest_words[3] <= before:
            assert time.monotonic() < deadline, 'Disabled OSC 52 output was not parsed'
            published = observer.poll()
            if published:
                published.close()
        assert not observer.latest_words[18] & 8, 'Revoked controller policy still accepted OSC 52'
        controller.sendall(packet(recovered + 4, CLIPBOARD))
        try:
            receive(controller, recovered + 4, CLIPBOARD)
        except OSError as error:
            assert error.errno == errno.EPERM
        else:
            raise AssertionError('Revoked controller fetched clipboard')
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
    subprocess.run([str(ROOT / 'batty'), '--terminate', 'clipboard', '--session-dir', str(root)],
                   cwd=ROOT, env=env, capture_output=True, timeout=8, check=True)

assert not (mirror_root / 'relay.sock').exists()
print('PASS OSC 52 remote clipboard, sealed reply, observer denial and policy revocation')
