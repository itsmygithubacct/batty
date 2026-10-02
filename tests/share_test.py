#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A fresh shared desktop exposes live Batty panes through authenticated video."""
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from Cryptodome.Cipher import DES
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from control import request  # noqa: E402
from kilix_share import runtime_parent  # noqa: E402

with patch.dict(os.environ, {'KILIX_SESSION_HOME': '/tmp/' + 'long-' * 20}), \
        patch('kilix_share.tempfile.gettempdir', return_value='/tmp'):
    assert runtime_parent() == Path('/tmp')


def receive(peer, count):
    data = bytearray()
    while len(data) < count:
        part = peer.recv(count - len(data))
        if not part:
            raise EOFError('Share VNC connection closed')
        data.extend(part)
    return bytes(data)


def vnc_input(port, password, line):
    with socket.create_connection(('127.0.0.1', port), timeout=4) as peer:
        peer.settimeout(4)
        assert receive(peer, 12) == b'RFB 003.008\n'
        peer.sendall(b'RFB 003.008\n')
        options = receive(peer, receive(peer, 1)[0])
        assert 2 in options
        peer.sendall(b'\x02')
        key = bytes(int(f'{byte:08b}'[::-1], 2) for byte in password.encode()[:8].ljust(8, b'\0'))
        peer.sendall(DES.new(key, DES.MODE_ECB).encrypt(receive(peer, 16)))
        assert struct.unpack('>I', receive(peer, 4))[0] == 0
        peer.sendall(b'\x01')
        header = receive(peer, 24)
        name_length = struct.unpack('>I', header[20:24])[0]
        assert name_length <= 256
        receive(peer, name_length)
        for char in line + '\n':
            symbol = 0xff0d if char == '\n' else ord(char)
            peer.sendall(struct.pack('>BBHI', 4, 1, 0, symbol))
            peer.sendall(struct.pack('>BBHI', 4, 0, 0, symbol))

with tempfile.TemporaryDirectory(prefix='bt-share-') as directory:
    base = Path(directory)
    runtime, storage = base / 'runtime', base / 'storage'
    runtime.mkdir(mode=0o700)
    storage.mkdir(mode=0o700)
    environment = os.environ | {'XDG_RUNTIME_DIR': str(runtime),
                                'BATTY_KILIX_STORAGE_HOME': str(storage),
                                'BATTY_OFFLINE': '1'}
    with patch.dict(os.environ, {'KILIX_SESSION_HOME': str(runtime / 'batty-apps')}):
        share_parent = runtime_parent()
    launched_at = time.time() - 1
    with (base / 'share.log').open('wb') as log:
        provider = subprocess.Popen([str(ROOT / 'kilix'), 'share', '--size', '800x600',
                                     '--fps', '5'], cwd=ROOT, env=environment,
                                    stdout=log, stderr=subprocess.STDOUT)
        session = runtime / 'batty-apps/stream' / f'run-{provider.pid}'
        child = None
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and provider.poll() is None:
                connections = session / 'connect.txt'
                roots = [path for path in share_parent.glob(
                    'batty-share-*/control/front-*/control.sock')
                    if path.stat().st_mtime >= launched_at]
                if connections.exists() and roots:
                    try:
                        panes = request(str(roots[0]), 'list')['panes']
                        if panes and panes[0]['pid'] > 0:
                            break
                    except (OSError, RuntimeError, ValueError):
                        pass
                time.sleep(0.1)
            assert provider.poll() is None and connections.exists() and roots and panes
            assert len(panes) == 1 and panes[0]['session'].startswith('kilix-auto-')
            child = panes[0]['pid']
            assert panes[0]['session_dir'].startswith(str(roots[0].parents[2]))
            connection_lines = connections.read_text().splitlines()
            vnc_port = int(next(line for line in connection_lines if line.startswith('VNC')).rsplit(':', 1)[1])
            vnc_password = next(line for line in connection_lines if line.startswith('control pw')).split()[-1]
            vnc_input(vnc_port, vnc_password, 'echo SHARE_INPUT_OK')
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if 'SHARE_INPUT_OK' in request(str(roots[0]), 'dump', panes[0]['id']):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError('Remote VNC keyboard did not reach Batty pane')
            address = next(line.split(maxsplit=1)[1] for line in connection_lines
                           if line.startswith('hls'))
            parsed = urllib.parse.urlsplit(address)
            assert parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
            token = urllib.parse.parse_qs(parsed.query)['t'][0]
            root = f'{parsed.scheme}://{parsed.netloc}'
            try:
                urllib.request.urlopen(root + '/hls/live.m3u8', timeout=4)
            except urllib.error.HTTPError as error:
                assert error.code == 401
            else:
                raise AssertionError('Shared video accepted an anonymous viewer')
            with urllib.request.urlopen(root + '/?t=' + token, timeout=4) as response:
                assert response.status == 200
                cookie = response.headers['Set-Cookie'].split(';', 1)[0]
            playlist = b''
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(urllib.request.Request(
                            root + '/hls/live.m3u8', headers={'Cookie': cookie}), timeout=4) as response:
                        playlist = response.read()
                except urllib.error.HTTPError as error:
                    assert error.code == 404
                if b'.m4s' in playlist:
                    break
                time.sleep(0.1)
            assert b'.m4s' in playlist
        except Exception:
            log.flush()
            print((base / 'share.log').read_text(errors='replace'), file=sys.stderr)
            raise
        finally:
            provider.terminate()
            try:
                provider.wait(timeout=10)
            except subprocess.TimeoutExpired:
                provider.kill()
                provider.wait(timeout=3)
        assert provider.returncode == 0 and not session.exists()
        deadline = time.monotonic() + 3
        while child is not None and Path('/proc', str(child)).exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child is not None and not Path('/proc', str(child)).exists()
        assert not any(path.parents[2].exists() for path in roots)

print('PASS nested Batty Kilix desktop, remote input, authenticated HLS and cleanup')
