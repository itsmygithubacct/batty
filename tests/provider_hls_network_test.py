#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Live Kilix LAN/TLS HLS authentication and playable video."""
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent


with tempfile.TemporaryDirectory(prefix='bt-provider-hls-') as directory:
    base = Path(directory)
    for name in ('runtime', 'storage', 'app'):
        (base / name).mkdir(mode=0o700)
    env = os.environ | {'XDG_RUNTIME_DIR': str(base / 'runtime'),
                        'BATTY_KILIX_STORAGE_HOME': str(base / 'storage'),
                        'BATTY_PROVIDER_TEST': str(base / 'app')}
    with (base / 'provider.log').open('wb') as log:
        provider = subprocess.Popen([str(ROOT / 'kilix'), 'run', '--lan', '--hls',
                                     '--no-pane', '--size', '320x240', '--', sys.executable,
                                     str(ROOT / 'tests/provider_app.py')],
                                    cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        session = base / 'runtime' / 'batty-apps' / 'stream' / f'run-{provider.pid}'
        try:
            # A fresh private root first prepares pinned browser assets.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and provider.poll() is None:
                if (session / 'connect.txt').exists() and (base / 'app/app.json').exists():
                    break
                time.sleep(0.05)
            assert provider.poll() is None and (session / 'connect.txt').exists(), 'LAN/HLS provider did not start'
            connection = (session / 'connect.txt').read_text().splitlines()
            address = next(line.split(maxsplit=1)[1] for line in connection if line.startswith('hls'))
            assert address.startswith('https://127.0.0.1:')
            parsed = urllib.parse.urlsplit(address)
            token = urllib.parse.parse_qs(parsed.query)['t'][0]
            tls = ssl.create_default_context()
            tls.check_hostname = False
            tls.verify_mode = ssl.CERT_NONE  # The provider intentionally uses a private self-signed certificate.
            root = f'{parsed.scheme}://{parsed.netloc}'

            def get(path, cookie=None):
                headers = {'Cookie': cookie} if cookie else {}
                with urllib.request.urlopen(urllib.request.Request(root + path, headers=headers),
                                            context=tls, timeout=4) as response:
                    return response.status, response.headers, response.read()

            try:
                get('/hls/live.m3u8')
            except urllib.error.HTTPError as error:
                assert error.code == 401
            else:
                raise AssertionError('Unauthenticated HLS playlist was accepted')
            try:
                get('/?t=wrong')
            except urllib.error.HTTPError as error:
                assert error.code == 401
            else:
                raise AssertionError('Incorrect browser token was accepted')
            status, headers, _ = get('/?t=' + token)
            assert status == 200
            cookie = headers['Set-Cookie'].split(';', 1)[0]
            assert cookie.startswith('kilix_t=')
            playlist = b''
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                try:
                    status, _, playlist = get('/hls/live.m3u8', cookie)
                except urllib.error.HTTPError as error:
                    if error.code != 404:
                        raise
                    time.sleep(0.1)
                    continue
                if b'#EXT-X-MAP:' in playlist and b'.m4s' in playlist:
                    break
                time.sleep(0.1)
            assert b'#EXT-X-MAP:' in playlist and b'.m4s' in playlist, playlist
            lines = playlist.decode().splitlines()
            init = next(line.split('URI="', 1)[1].split('"', 1)[0]
                        for line in lines if line.startswith('#EXT-X-MAP:'))
            segments = [line for line in lines if line.endswith('.m4s')]
            assert segments
            _, _, init_data = get('/hls/' + init, cookie)
            assert b'ftyp' in init_data[:32]
            _, _, segment_data = get('/hls/' + segments[-1], cookie)
            assert b'moof' in segment_data[:128]
            media = base / 'sample.mp4'
            media.write_bytes(init_data + segment_data)
            probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                                    'stream=codec_type,codec_name', '-of', 'json', str(media)],
                                   capture_output=True, text=True, timeout=8, check=True)
            streams = {(item['codec_type'], item['codec_name'])
                       for item in json.loads(probe.stdout)['streams']}
            assert ('video', 'h264') in streams, streams
            decoded = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(media),
                                      '-frames:v', '1', '-f', 'null', '-'],
                                     capture_output=True, timeout=8)
            assert decoded.returncode == 0, decoded.stderr.decode(errors='replace')
            app_pid = json.loads((base / 'app/app.json').read_text())['pid']
        except Exception:
            log.flush()
            print((base / 'provider.log').read_text(errors='replace'), file=sys.stderr)
            bridge = session / 'bridge.log'
            if bridge.exists():
                print(bridge.read_text(errors='replace'), file=sys.stderr)
            raise
        finally:
            provider.terminate()
            try:
                provider.wait(timeout=7)
            except subprocess.TimeoutExpired:
                provider.kill()
                provider.wait(timeout=3)
        assert provider.returncode == 0 and not session.exists(), 'LAN/HLS provider did not clean up'
        assert not Path('/proc', str(app_pid)).exists(), 'LAN/HLS app survived provider exit'
        log.flush()
        instructions = (base / 'provider.log').read_text(errors='replace')
        assert 'then open  https://localhost:' in instructions
        assert 'view (mpv) : mpv https://localhost:' in instructions

print('PASS TLS HLS token/cookie authentication, playable H.264 and cleanup')
