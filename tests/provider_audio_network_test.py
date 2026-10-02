#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Loopback Kilix MSE stream with real video and audible AAC capture."""
import array
import asyncio
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

from websockets.legacy.client import connect

ROOT = Path(__file__).resolve().parent.parent


def pulse_server():
    info = subprocess.run(['pactl', 'info'], capture_output=True, text=True, timeout=5, check=True)
    return next(line.split(': ', 1)[1] for line in info.stdout.splitlines()
                if line.startswith('Server String: '))


async def stream(port, token):
    try:
        async with connect(f'ws://127.0.0.1:{port}/ts', open_timeout=3):
            raise AssertionError('Unauthenticated MSE stream was accepted')
    except Exception as error:
        if getattr(error, 'status_code', None) != 401:
            raise
    parts = []
    size = 0
    deadline = time.monotonic() + 12
    async with connect(f'ws://127.0.0.1:{port}/ts?t={token}', max_size=None,
                       open_timeout=3) as socket:
        while time.monotonic() < deadline and size < 2000000:
            part = await asyncio.wait_for(socket.recv(), timeout=4)
            assert isinstance(part, bytes) and len(part) % 188 == 0
            parts.append(part)
            size += len(part)
    return b''.join(parts)


with tempfile.TemporaryDirectory(prefix='bt-provider-audio-') as directory:
    base = Path(directory)
    for name in ('runtime', 'storage', 'app'):
        (base / name).mkdir(mode=0o700)
    env = os.environ | {'PULSE_SERVER': pulse_server(),
                        'XDG_RUNTIME_DIR': str(base / 'runtime'),
                        'BATTY_KILIX_STORAGE_HOME': str(base / 'storage'),
                        'BATTY_PROVIDER_TEST': str(base / 'app')}
    with (base / 'provider.log').open('wb') as log:
        provider = subprocess.Popen([str(ROOT / 'kilix'), 'run', '--mse', '--audio',
                                     '--no-pane', '--size', '320x240', '--', sys.executable,
                                     str(ROOT / 'tests/provider_app.py')],
                                    cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        session = base / 'runtime' / 'batty-apps' / 'stream' / f'run-{provider.pid}'
        connect_file = session / 'connect.txt'
        app_file = base / 'app' / 'app.json'
        audio_result = []
        sound = None
        try:
            # A fresh private root first prepares pinned browser assets.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and provider.poll() is None:
                if connect_file.exists() and app_file.exists():
                    break
                time.sleep(0.05)
            assert connect_file.exists() and app_file.exists(), (
                f'MSE provider did not start (exit={provider.poll()}, '
                f'connect={connect_file.exists()}, app={app_file.exists()}): '
                f'{(base / "provider.log").read_text(errors="replace")[-4000:]}')
            browser = next(line.split(maxsplit=1)[1] for line in connect_file.read_text().splitlines()
                           if line.startswith('browser'))
            parsed = urllib.parse.urlsplit(browser)
            token = urllib.parse.parse_qs(parsed.query)['t'][0]
            sink = f'kilix_run_{provider.pid}'
            tone = bytearray()
            for index in range(48000 * 10):
                value = int(10000 * math.sin(index * 2 * math.pi * 440 / 48000))
                tone.extend(struct.pack('<hh', value, value))

            def play():
                done = subprocess.run(['pacat', '--playback', '--raw', '--format=s16le',
                                       '--rate=48000', '--channels=2'],
                                      input=bytes(tone), env=env | {'PULSE_SINK': sink},
                                      capture_output=True, timeout=18)
                audio_result.append(done.returncode)

            sound = threading.Thread(target=play)
            sound.start()
            data = asyncio.run(stream(parsed.port, token))
            sound.join(timeout=18)
            assert not sound.is_alive() and audio_result == [0], 'PulseAudio source did not finish'
            assert len(data) >= 25000 and data[:1] == b'\x47', 'MSE stream carried no MPEG-TS'
            transport = base / 'stream.ts'
            transport.write_bytes(data)
            packets = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_packets', '-show_entries',
                 'packet=stream_index,pts_time', '-of', 'json', str(transport)],
                capture_output=True, text=True, timeout=8, check=True)
            timelines = {0: [], 1: []}
            for packet in json.loads(packets.stdout)['packets']:
                index = packet.get('stream_index')
                if index in timelines and packet.get('pts_time') not in (None, 'N/A'):
                    timelines[index].append(float(packet['pts_time']))
            video, audio = timelines[0], timelines[1]
            assert len(video) >= 20 and len(audio) >= 300, (
                'MSE A/V packets stopped early', len(video), len(audio))
            assert abs(video[0] - audio[0]) < 0.2, (
                'MSE A/V clocks start apart', video[0], audio[0])
            assert audio[-1] >= 8 and video[-1] >= audio[-1] - 3, (
                'MSE video keepalives stopped while audio continued', video[-1], audio[-1])
            assert max(right - left for left, right in zip(video, video[1:])) < 3, (
                'MSE video keepalive gap exceeded three seconds', video)
            probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                                    'stream=codec_type,codec_name', '-of', 'json', str(transport)],
                                   capture_output=True, text=True, timeout=8, check=True)
            tracks = {(item['codec_type'], item['codec_name'])
                      for item in json.loads(probe.stdout)['streams']}
            assert {('video', 'h264'), ('audio', 'aac')} <= tracks, tracks
            decoded = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(transport), '-map', '0:a:0',
                                      '-f', 's16le', '-ac', '1', '-ar', '48000', '-'],
                                     capture_output=True, timeout=8, check=True)
            samples = array.array('h')
            samples.frombytes(decoded.stdout)
            if sys.byteorder != 'little':
                samples.byteswap()
            assert len(samples) > 48000 // 4
            rms = math.sqrt(sum(value * value for value in samples) / len(samples))
            assert rms > 1000, ('Decoded AAC was silent', rms)
            app_pid = json.loads(app_file.read_text())['pid']
        finally:
            provider.terminate()
            try:
                provider.wait(timeout=7)
            except subprocess.TimeoutExpired:
                provider.kill()
                provider.wait(timeout=3)
            if sound is not None and sound.is_alive():
                sound.join(timeout=2)
        assert provider.returncode == 0 and not session.exists(), 'MSE provider did not clean up'
        assert not Path('/proc', str(app_pid)).exists(), 'MSE app survived provider exit'
        sinks = subprocess.run(['pactl', 'list', 'short', 'sinks'], env=env,
                               capture_output=True, text=True, timeout=5, check=True)
        assert sink not in sinks.stdout, 'Private PulseAudio sink survived provider exit'

print('PASS authenticated MSE H.264/AAC, audible tone and private sink cleanup')
