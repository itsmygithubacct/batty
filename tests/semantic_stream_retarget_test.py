#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A live native viewer and input peer survive new frame/input ports and token."""
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
from kilix_input_source import SEND
from kilix_stream_mirror import StreamMirror
from kilix_stream_server import StreamServer


def text(frame):
    head = os.pread(frame.fd, 76, 0)
    cells, count = struct.unpack_from('<II', head, 52)
    title = struct.unpack_from('<I', head, 68)[0]
    body = os.pread(frame.fd, count * 4, 860 + title + cells * 31)
    return ''.join(chr(value) for value in struct.unpack('<' + 'I' * count, body))


def send(connection, number, body, expected=0):
    fd = os.memfd_create('batty-retarget-input', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.write(fd, body)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, SEALS)
        words = list(HEADER.unpack(packet(number, SEND)))
        words[6] = len(body)
        connection.sendmsg([HEADER.pack(*words)],
                           [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array('i', [fd]).tobytes())])
        try:
            response, returned = receive(connection, number, SEND)
        except OSError as error:
            assert expected and error.errno == expected
        else:
            assert expected == 0 and response[11] == 0 and returned < 0
    finally:
        os.close(fd)


root = Path(os.environ['BATTY_TEST_SESSION_DIR'])
mirror_root = root / 'retarget-mirror'
env = os.environ | {'BATTY_CONFIG': '/dev/null'}
owner = subprocess.Popen([str(ROOT / 'batty'), '--headless', '--session', 'retarget',
                          '--session-dir', str(root), '--', '/bin/cat'],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
servers = []
rogue_owner = None
mirror = viewer = controller = None
mirror_thread = None
try:
    deadline = time.monotonic() + 10
    while not (root / 'retarget.sock').exists():
        assert owner.poll() is None and time.monotonic() < deadline
        time.sleep(0.02)

    def start_server(name='retarget'):
        server = StreamServer(root, name)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        servers.append((server, worker))
        return server

    first = start_server()
    mirror = StreamMirror('127.0.0.1', first.port, first.token, mirror_root, 'relay',
                          input_port=first.input_port)
    mirror_thread = threading.Thread(target=mirror.serve_forever, daemon=True)
    mirror_thread.start()
    mirror.ready()
    mirror.connect_input()
    viewer = subprocess.Popen([str(ROOT / 'batty'), '--headless', '--observe', 'relay',
                               '--session-dir', str(mirror_root)],
                              cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    controller = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    controller.settimeout(5)
    controller.connect(str(mirror_root / 'relay.sock'))
    controller.sendall(packet(1, HELLO, flags=1))
    words, descriptor = receive(controller, 1, HELLO)
    assert descriptor >= 0
    os.close(descriptor)
    original_epoch = words[2]

    with FrameSource(mirror_root, 'relay') as observer:
        deadline = time.monotonic() + 5
        while True:
            with mirror.condition:
                attached = len(mirror.clients) >= 3  # controller, observer and native viewer
            if attached:
                break
            assert viewer.poll() is None and time.monotonic() < deadline, 'Native viewer attachment deadline'
            time.sleep(0.02)
        send(controller, 2, b'BEFORE_RETARGET\n')
        deadline = time.monotonic() + 5
        seen = text(observer.frame)
        while 'BEFORE_RETARGET' not in seen:
            assert time.monotonic() < deadline
            frame = observer.poll()
            if frame:
                with frame:
                    seen = text(frame)
        first.close()
        servers.pop()[1].join(timeout=3)
        deadline = time.monotonic() + 5
        while mirror.connected:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        second = start_server()

        def move(server, token, with_input=True):
            command = [str(ROOT / 'kilix'), 'remote', 'retarget', '--name', 'relay',
                       '--session-dir', str(mirror_root), '--port', str(server.port),
                       '--token', token.hex()]
            if with_input:
                command.extend(['--input-port', str(server.input_port)])
            return subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=8)

        rejected = move(second, second.token, with_input=False)
        assert rejected.returncode != 0 and 'input role' in rejected.stderr
        assert mirror.port == first.port
        wrong_token = move(second, b'\0' * 16)
        assert wrong_token.returncode != 0 and 'rejected' in wrong_token.stderr
        deadline = time.monotonic() + 5
        while mirror.failed is None:
            assert time.monotonic() < deadline, 'Wrong token did not fail authentication'
            time.sleep(0.02)
        rogue_owner = subprocess.Popen([str(ROOT / 'batty'), '--headless', '--session', 'other-owner',
                                        '--session-dir', str(root), '--', '/bin/cat'],
                                       cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 5
        while not (root / 'other-owner.sock').exists():
            assert rogue_owner.poll() is None and time.monotonic() < deadline
            time.sleep(0.02)
        rogue = start_server('other-owner')
        wrong_owner = move(rogue, rogue.token)
        assert wrong_owner.returncode != 0 and 'owner' in wrong_owner.stderr
        deadline = time.monotonic() + 5
        while mirror.failed is None:
            assert time.monotonic() < deadline, 'Different owner epoch was accepted'
            time.sleep(0.02)
        assert 'epoch' in str(mirror.failed) or 'owner' in str(mirror.failed)
        assert viewer.poll() is None and mirror.metadata[2] == original_epoch, (
            viewer.poll(), viewer.stderr.read().decode('utf-8', 'replace') if viewer.poll() is not None else '',
            mirror.metadata[2], original_epoch, mirror.connected, mirror.failed)
        with FrameSource(mirror_root, 'relay') as late:
            assert 'BEFORE_RETARGET' in text(late.frame), 'Late observer sees retained frame during rejected route'
        send(controller, 3, b'WRONG_OWNER_INPUT\n', expected=errno.ENOTCONN)
        assert move(second, second.token).returncode == 0
        deadline = time.monotonic() + 5
        while not mirror.connected:
            assert time.monotonic() < deadline, 'New server did not reconnect'
            time.sleep(0.02)
        assert mirror.metadata[2] == original_epoch and viewer.poll() is None
        assert mirror.reconnects >= 1
        send(controller, 4, b'AFTER_RETARGET\n')
        deadline = time.monotonic() + 5
        while 'AFTER_RETARGET' not in seen:
            assert time.monotonic() < deadline, 'Retargeted input did not reach original PTY'
            frame = observer.poll()
            if frame:
                with frame:
                    seen = text(frame)
        assert owner.poll() is None and viewer.poll() is None
finally:
    if controller is not None:
        controller.close()
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
    for server, worker in servers:
        server.close()
        worker.join(timeout=3)
    if rogue_owner is not None:
        rogue_owner.terminate()
        try:
            rogue_owner.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            rogue_owner.kill()
            rogue_owner.communicate(timeout=3)
        subprocess.run([str(ROOT / 'batty'), '--terminate', 'other-owner', '--session-dir', str(root)],
                       cwd=ROOT, env=env, capture_output=True, timeout=8, check=True)
    owner.terminate()
    try:
        owner.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        owner.kill()
        owner.communicate(timeout=3)
    subprocess.run([str(ROOT / 'batty'), '--terminate', 'retarget', '--session-dir', str(root)],
                   cwd=ROOT, env=env, capture_output=True, timeout=8, check=True)

assert not (mirror_root / 'relay.sock').exists()
assert not (mirror_root / 'relay.control.sock').exists()
print('PASS public retarget: new token/ports, same native viewer, owner epoch and input peer')
