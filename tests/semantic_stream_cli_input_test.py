#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""The public remote commands attach an interactive native Batty viewer."""
import errno
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from kilix_frame_source import FrameSource, HELLO, packet, receive


def content(frame):
    header = os.pread(frame.fd, 76, 0)
    cells, count = struct.unpack_from('<II', header, 52)
    title = struct.unpack_from('<I', header, 68)[0]
    raw = os.pread(frame.fd, count * 4, 860 + title + cells * 31)
    return ''.join(chr(value) for value in struct.unpack('<' + 'I' * count, raw))


root = Path(os.environ['BATTY_TEST_SESSION_DIR'])
mirror_root = root / 'cli-mirror'
env = os.environ | {'BATTY_CONFIG': '/dev/null'}
owner = subprocess.Popen([str(ROOT / 'batty'), '--headless', '--session', 'cli-input',
                          '--session-dir', str(root), '--', '/bin/cat'],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
server = viewer = None
try:
    deadline = time.monotonic() + 10
    while not (root / 'cli-input.sock').exists():
        assert owner.poll() is None and time.monotonic() < deadline
        time.sleep(0.02)
    server = subprocess.Popen([str(ROOT / 'kilix'), 'remote', 'serve', '--session', 'cli-input',
                               '--session-dir', str(root)],
                              cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    first = server.stderr.readline().decode('ascii', 'replace').strip().split()
    second = server.stderr.readline().decode('ascii', 'replace').strip().split()
    assert (len(first) == 5 and first[:2] == ['kilix', 'remote:'] and first[3] == 'token' and
            len(second) == 4 and second[:3] == ['kilix', 'remote:', 'input-port'])
    port, token, input_port = first[2].rsplit(':', 1)[1], first[4], second[3]
    assert len(token) == 32
    command = [str(ROOT / 'kilix'), 'remote', 'view', '--port', port,
               '--input-port', input_port, '--token', token, '--session-dir', str(mirror_root)]
    if not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY'):
        command.append('--headless')
    viewer = subprocess.Popen(command,
                              cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    mirror_name = f'remote-{viewer.pid}'
    endpoint = mirror_root / f'{mirror_name}.sock'
    deadline = time.monotonic() + 8
    while not endpoint.exists():
        if viewer.poll() is not None:
            raise AssertionError('Interactive viewer exited: ' + viewer.stderr.read().decode('utf-8', 'replace'))
        assert time.monotonic() < deadline, 'Interactive viewer did not start'
        time.sleep(0.02)
    deadline = time.monotonic() + 5
    while True:
        try:
            observer = FrameSource(mirror_root, mirror_name)
            break
        except (ConnectionError, OSError):
            assert viewer.poll() is None and time.monotonic() < deadline, 'Interactive mirror did not become ready'
            time.sleep(0.02)
    with observer:
        assert observer.epoch and content(observer.frame) is not None
        deadline = time.monotonic() + 5
        while True:
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as rival:
                rival.settimeout(3)
                rival.connect(str(endpoint))
                rival.sendall(packet(1, HELLO, flags=1))
                try:
                    _, descriptor = receive(rival, 1, HELLO)
                    if descriptor >= 0:
                        os.close(descriptor)
                except OSError as error:
                    if error.errno == errno.EBUSY:
                        break
                    raise
            assert viewer.poll() is None and time.monotonic() < deadline
            time.sleep(0.02)
    assert owner.poll() is None and viewer.poll() is None
finally:
    for process in (viewer, server, owner):
        if process is not None:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)
    subprocess.run([str(ROOT / 'batty'), '--terminate', 'cli-input', '--session-dir', str(root)],
                   cwd=ROOT, env=env, capture_output=True, timeout=8, check=True)

assert not endpoint.exists()
print('PASS public interactive remote commands, native controller and persistent PTY owner')
