#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Real xterm through the bundled Kilix provider and authenticated VNC."""
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
            raise EOFError('xterm VNC connection closed')
        data.extend(part)
    return bytes(data)


def connect(port, password):
    sock = socket.create_connection(('127.0.0.1', port), timeout=4)
    sock.settimeout(5)
    assert receive(sock, 12) == b'RFB 003.008\n'
    sock.sendall(b'RFB 003.008\n')
    choices = receive(sock, receive(sock, 1)[0])
    assert 2 in choices, choices
    sock.sendall(b'\x02')
    challenge = receive(sock, 16)
    key = bytes(int(f'{byte:08b}'[::-1], 2) for byte in password.encode()[:8].ljust(8, b'\0'))
    sock.sendall(DES.new(key, DES.MODE_ECB).encrypt(challenge))
    assert struct.unpack('>I', receive(sock, 4))[0] == 0
    sock.sendall(b'\x01')
    header = receive(sock, 24)
    width, height = struct.unpack('>HH', header[:4])
    assert header[4] == 32, 'Expected the provider 32-bit VNC framebuffer'
    name_length = struct.unpack('>I', header[20:24])[0]
    assert name_length < 256
    receive(sock, name_length)
    return sock, width, height


def frame(sock, width, height):
    sock.sendall(struct.pack('>BBHi', 2, 0, 1, 0))  # Raw encoding.
    sock.sendall(struct.pack('>BBHHHH', 3, 0, 0, 0, width, height))
    header = receive(sock, 4)
    assert header[0] == 0
    rectangles = struct.unpack('>H', header[2:])[0]
    assert 0 < rectangles <= 64
    colors = set()
    for _ in range(rectangles):
        x, y, columns, rows, encoding = struct.unpack('>HHHHi', receive(sock, 12))
        assert encoding == 0 and columns and rows and x + columns <= width and y + rows <= height
        pixels = receive(sock, columns * rows * 4)
        if len(colors) < 3:
            for at in range(0, len(pixels), 4):
                colors.add(pixels[at:at + 4])
                if len(colors) >= 3:
                    break
    return len(colors)


def key(sock, keysym):
    for pressed in (1, 0):
        sock.sendall(struct.pack('>BBHI', 4, pressed, 0, keysym))


with tempfile.TemporaryDirectory(prefix='bt-provider-xterm-') as directory:
    base = Path(directory)
    for name in ('runtime', 'storage'):
        (base / name).mkdir(mode=0o700)
    output = base / 'received.txt'
    env = os.environ | {'XDG_RUNTIME_DIR': str(base / 'runtime'),
                        'BATTY_KILIX_STORAGE_HOME': str(base / 'storage'),
                        'BATTY_REAL_APP_OUTPUT': str(output)}
    with (base / 'provider.log').open('w+') as log:
        provider = subprocess.Popen([str(ROOT / 'kilix'), 'run', '--serve', '--no-pane',
                                     '--size', '400x300', '--', 'xterm', '-fa', 'Monospace',
                                     '-fs', '14', '-geometry', '40x10', '-e', '/bin/sh', '-c',
                                     'read -r line; printf "%s" "$line" > "$BATTY_REAL_APP_OUTPUT"; sleep 2'],
                                    cwd=ROOT, env=env, stdout=log, stderr=log)
        session = base / 'runtime' / 'batty-apps' / 'stream' / f'run-{provider.pid}'
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and provider.poll() is None:
                if (session / 'connect.txt').exists():
                    break
                time.sleep(0.05)
            assert (session / 'connect.txt').exists(), 'Real xterm provider did not start'
            instructions = (session / 'connect.txt').read_text().splitlines()
            port = int(next(line for line in instructions if line.startswith('VNC')).rsplit(':', 1)[1])
            password = next(line for line in instructions if line.startswith('control pw')).split()[-1]
            deadline = time.monotonic() + 8
            while True:
                assert provider.poll() is None, f'xterm provider exited early: {provider.returncode}'
                try:
                    viewer, width, height = connect(port, password)
                    break
                except (ConnectionRefusedError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            try:
                assert (width, height) == (400, 300)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and frame(viewer, width, height) < 2:
                    time.sleep(0.1)
                assert frame(viewer, width, height) >= 2, 'xterm produced no varied framebuffer pixels'
                # Xvnc can expose a varied root framebuffer before the xterm
                # child maps its window. Wait for the real application rather
                # than sending keys into an empty display during startup.
                auth = next(session.glob('Xauth-*'))
                display = ':' + auth.name.removeprefix('Xauth-')
                deadline = time.monotonic() + 5
                while True:
                    windows = subprocess.run(['xwininfo', '-display', display, '-root', '-tree'],
                                             env=env | {'XAUTHORITY': str(auth)},
                                             capture_output=True, text=True, timeout=2)
                    if windows.returncode == 0 and 'xterm' in windows.stdout.lower():
                        break
                    assert time.monotonic() < deadline, 'xterm window did not map'
                    time.sleep(0.1)
                viewer.sendall(struct.pack('>BBHH', 5, 1, 120, 100))
                viewer.sendall(struct.pack('>BBHH', 5, 0, 120, 100))
                for char in 'REAL_XTERM_INPUT':
                    key(viewer, ord(char))
                key(viewer, 0xff0d)
                deadline = time.monotonic() + 7
                while time.monotonic() < deadline and not output.exists():
                    time.sleep(0.05)
                assert output.exists() and output.read_text() == 'REAL_XTERM_INPUT', \
                    'Authenticated VNC typing did not reach real xterm'
            finally:
                viewer.close()
            provider.wait(timeout=7)
            assert provider.returncode == 0 and not session.exists(), 'xterm provider did not clean up'
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read(), file=sys.stderr)
            raise
        finally:
            if provider.poll() is None:
                provider.terminate()
                try:
                    provider.wait(timeout=7)
                except subprocess.TimeoutExpired:
                    provider.kill()
                    provider.wait(timeout=3)

print('PASS real xterm pixels, authenticated typing, child exit and provider cleanup')
