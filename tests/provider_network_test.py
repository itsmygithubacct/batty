#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Live Kilix provider VNC pixels, controller input and view-only isolation."""
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time

from Cryptodome.Cipher import DES

ROOT = Path(__file__).resolve().parent.parent


def receive(sock, count):
    data = bytearray()
    while len(data) < count:
        part = sock.recv(count - len(data))
        if not part:
            raise EOFError('VNC connection closed during a message')
        data.extend(part)
    return bytes(data)


def connect(port, password):
    sock = socket.create_connection(('127.0.0.1', port), timeout=4)
    sock.settimeout(4)
    try:
        assert receive(sock, 12) == b'RFB 003.008\n'
        sock.sendall(b'RFB 003.008\n')
        security = receive(sock, receive(sock, 1)[0])
        assert 2 in security, security  # VncAuth is required, not an unauthenticated viewer.
        sock.sendall(b'\x02')
        challenge = receive(sock, 16)
        key = bytes(int(f'{byte:08b}'[::-1], 2) for byte in password.encode()[:8].ljust(8, b'\0'))
        sock.sendall(DES.new(key, DES.MODE_ECB).encrypt(challenge))
        assert struct.unpack('>I', receive(sock, 4))[0] == 0
        sock.sendall(b'\x01')
        header = receive(sock, 24)
        width, height = struct.unpack('>HH', header[:4])
        name_length = struct.unpack('>I', header[20:24])[0]
        assert name_length <= 256
        receive(sock, name_length)
        return sock, width, height, header[4:20]
    except BaseException:
        sock.close()
        raise


def sample(sock, width, height, format_bytes, point=(250, 200)):
    assert format_bytes[0] == 32 and format_bytes[3] == 1
    sock.sendall(struct.pack('>BBHi', 2, 0, 1, 0))  # Raw encoding only.
    sock.sendall(struct.pack('>BBHHHH', 3, 0, 0, 0, width, height))
    header = receive(sock, 4)
    assert header[0] == 0
    rectangles = struct.unpack('>H', header[2:])[0]
    assert 0 < rectangles <= 64
    result = None
    for _ in range(rectangles):
        x, y, columns, rows, encoding = struct.unpack('>HHHHi', receive(sock, 12))
        assert encoding == 0 and 0 < columns <= width and 0 < rows <= height
        assert x + columns <= width and y + rows <= height
        pixels = receive(sock, columns * rows * 4)
        if x <= point[0] < x + columns and y <= point[1] < y + rows:
            offset = ((point[1] - y) * columns + point[0] - x) * 4
            value = int.from_bytes(pixels[offset:offset + 4], 'big' if format_bytes[2] else 'little')
            result = tuple((value >> format_bytes[index]) & 255 for index in (10, 11, 12))
    assert result is not None, 'VNC frame omitted the application sample'
    return result


with tempfile.TemporaryDirectory(prefix='bt-provider-network-') as directory:
    base = Path(directory)
    for name in ('runtime', 'storage', 'app'):
        (base / name).mkdir(mode=0o700)
    env = os.environ | {'XDG_RUNTIME_DIR': str(base / 'runtime'),
                        'BATTY_KILIX_STORAGE_HOME': str(base / 'storage'),
                        'BATTY_PROVIDER_TEST': str(base / 'app')}
    with (base / 'provider.log').open('wb') as log:
        provider = subprocess.Popen([str(ROOT / 'kilix'), 'run', '--serve', '--no-pane',
                                     '--size', '320x240', '--', sys.executable,
                                     str(ROOT / 'tests/provider_app.py')],
                                    cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        session = base / 'runtime' / 'batty-apps' / 'stream' / f'run-{provider.pid}'
        connect_file = session / 'connect.txt'
        app_file = base / 'app' / 'app.json'
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and provider.poll() is None:
                if connect_file.exists() and app_file.exists():
                    break
                time.sleep(0.05)
            assert connect_file.exists() and app_file.exists(), 'Network app did not start'
            connection = connect_file.read_text().splitlines()
            port = int(next(line for line in connection if line.startswith('VNC')).rsplit(':', 1)[1])
            control_password = next(line for line in connection if line.startswith('control pw')).split()[-1]
            view_password = next(line for line in connection if line.startswith('view-only')).split()[-1]
            controller, width, height, pixel_format = connect(port, control_password)
            try:
                assert (width, height) == (320, 240)
                assert sample(controller, width, height, pixel_format) == (34, 68, 102)
                controller.sendall(struct.pack('>BBHH', 5, 1, 20, 20))
                controller.sendall(struct.pack('>BBHH', 5, 0, 20, 20))
                events = base / 'app' / 'events.jsonl'
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if events.exists() and '"type": 5' in events.read_text():
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError('Authenticated VNC click did not reach the app')
                assert sample(controller, width, height, pixel_format) == (34, 170, 68)
            finally:
                controller.close()
            viewer, _, _, _ = connect(port, view_password)
            try:
                previous = events.read_text()
                viewer.sendall(struct.pack('>BBHH', 5, 1, 30, 30))
                viewer.sendall(struct.pack('>BBHH', 5, 0, 30, 30))
                time.sleep(0.5)
                assert events.read_text() == previous, 'View-only VNC input reached the app'
            finally:
                viewer.close()
        finally:
            provider.terminate()
            try:
                provider.wait(timeout=7)
            except subprocess.TimeoutExpired:
                provider.kill()
                provider.wait(timeout=3)
        assert provider.returncode == 0 and not session.exists(), 'Network provider did not clean up'

print('PASS authenticated VNC pixels, controller input, view-only isolation and cleanup')
