#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Authenticated PCM from a live pane reaches direct and public remote viewers."""
import os
from pathlib import Path
import shlex
import socket
import struct
import subprocess
import sys
import time
import zlib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from kilix_semantic_wire import AUTH_REJECT, AUTH_REQUEST, authenticate_client, exact, receive_header
from kilix_stream_audio import (BLOCK, HELLO, HELLO_MAGIC, MediaClock, RAW_MAGIC, ZIP_MAGIC,
                                encode_block, receive_block)


def source_command(sample):
    code = (f'import struct,sys; b=struct.pack("<h",{sample})*1920; '
            '[sys.stdout.buffer.write(b) for _ in range(180)]')
    return f'{shlex.quote(sys.executable)} -u -c {shlex.quote(code)}'


root = Path(os.environ['BATTY_TEST_SESSION_DIR'])
clock = MediaClock()
assert abs(clock.audio_deadline(1020, received=5.02) - 5.10) < 0.001
assert abs(clock.frame_deadline(1000, received=5.01) - 5.08) < 0.001
assert abs(clock.frame_deadline(1040, received=5.09) - 5.12) < 0.001
clock.reset()
assert abs(clock.audio_deadline(1060, received=8.0) - 8.08) < 0.001
assert abs(clock.frame_deadline(1080, received=8.01) - 8.09) < 0.001
clock.reset(2)
assert clock.audio_deadline(999000, received=9.0, generation=1) is None
assert abs(clock.frame_deadline(2000, received=9.0, generation=2) - 9.08) < 0.001
assert abs(clock.audio_deadline(2020, received=9.02, generation=2) - 9.10) < 0.001
mirror_root = root / 'audio-mirror'
pcm_path = root / 'heard.pcm'
env = os.environ | {'BATTY_CONFIG': '/dev/null'}
noise = os.urandom(3840)
raw_packet = encode_block(noise, 48000, 2, 20)
assert BLOCK.unpack(raw_packet[:BLOCK.size])[0] == RAW_MAGIC
writer, reader = socket.socketpair()
try:
    writer.sendall(raw_packet)
    assert receive_block(reader, 48000, 2) == (20, noise)
    bomb = zlib.compress(bytes(3844), 1)
    writer.sendall(BLOCK.pack(ZIP_MAGIC, 48000, 2, 40, 3840, len(bomb)) + bomb)
    try:
        receive_block(reader, 48000, 2)
    except ValueError:
        pass
    else:
        raise AssertionError('Audio decoder accepted output beyond the declared size')
finally:
    writer.close()
    reader.close()
owner = subprocess.Popen([str(ROOT / 'batty'), '--headless', '--session', 'audio',
                          '--session-dir', str(root), '--', '/bin/cat'],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
servers = []
viewer = None
try:
    deadline = time.monotonic() + 10
    while not (root / 'audio.sock').exists():
        assert owner.poll() is None and time.monotonic() < deadline
        time.sleep(0.02)

    def serve(sample):
        server = subprocess.Popen([str(ROOT / 'kilix'), 'remote', 'serve', '--session', 'audio',
                                   '--session-dir', str(root), '--audio-source', source_command(sample),
                                   '--audio-rate', '48000', '--audio-channels', '2'],
                                  cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE)
        servers.append(server)
        first = server.stderr.readline().decode('ascii', 'replace').strip().split()
        second = server.stderr.readline().decode('ascii', 'replace').strip().split()
        third = server.stderr.readline().decode('ascii', 'replace').strip().split()
        assert (len(first) == 5 and first[:2] == ['kilix', 'remote:'] and first[3] == 'token' and
                len(second) == 4 and second[:3] == ['kilix', 'remote:', 'input-port'] and
                len(third) == 4 and third[:3] == ['kilix', 'remote:', 'audio-port'])
        return int(first[2].rsplit(':', 1)[1]), bytes.fromhex(first[4]), int(third[3])

    port, token, audio_port = serve(12000)
    with socket.create_connection(('127.0.0.1', audio_port), timeout=3) as denied:
        denied.settimeout(3)
        denied.sendall(AUTH_REQUEST + bytes(16))
        assert exact(denied, 4) == AUTH_REJECT

    with socket.create_connection(('127.0.0.1', audio_port), timeout=3) as direct:
        direct.settimeout(5)
        authenticate_client(direct, token)
        magic, rate, channels, epoch = HELLO.unpack(exact(direct, HELLO.size))
        assert magic == HELLO_MAGIC and (rate, channels) == (48000, 2) and epoch
        moments = []
        for _ in range(5):
            when, pcm = receive_block(direct, rate, channels)
            moments.append(when)
            assert pcm == struct.pack('<h', 12000) * 1920
        assert moments == sorted(moments) and moments[-1] > moments[0]
        with socket.create_connection(('127.0.0.1', port), timeout=3) as frames:
            frames.settimeout(5)
            authenticate_client(frames, token)
            video = receive_header(frames)
            assert video[-1] and abs(video[-1] - moments[-1]) < 1000, (
                video[-1], moments[-1])
        with socket.create_connection(('127.0.0.1', audio_port), timeout=3) as second:
            second.settimeout(5)
            authenticate_client(second, token)
            assert HELLO.unpack(exact(second, HELLO.size)) == (HELLO_MAGIC, rate, channels, epoch)
            for _ in range(3):
                _, second_pcm = receive_block(second, rate, channels)
                _, first_pcm = receive_block(direct, rate, channels)
                assert first_pcm == second_pcm == struct.pack('<h', 12000) * 1920

    viewer = subprocess.Popen([str(ROOT / 'kilix'), 'remote', 'view', '--headless',
                               '--name', 'audio-relay', '--session-dir', str(mirror_root),
                               '--port', str(port), '--audio-port', str(audio_port),
                               '--token', token.hex(), '--audio-output',
                               f'cat >> {shlex.quote(str(pcm_path))}'],
                              cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 6
    while not pcm_path.exists() or pcm_path.stat().st_size < 10 * 3840:
        assert viewer.poll() is None and time.monotonic() < deadline, 'Remote viewer did not play PCM'
        time.sleep(0.02)
    before = pcm_path.read_bytes()
    assert struct.pack('<h', 12000) * 100 in before
    first_viewer_pid = viewer.pid
    servers[0].terminate()
    servers[0].communicate(timeout=5)
    port2, token2, audio_port2 = serve(-11000)
    missing_audio = subprocess.run([str(ROOT / 'kilix'), 'remote', 'retarget', '--name', 'audio-relay',
                                    '--session-dir', str(mirror_root), '--port', str(port2),
                                    '--token', token2.hex()],
                                   cwd=ROOT, env=env, capture_output=True, text=True, timeout=8)
    assert missing_audio.returncode != 0 and 'audio role' in missing_audio.stderr
    wrong_token = subprocess.run([str(ROOT / 'kilix'), 'remote', 'retarget', '--name', 'audio-relay',
                                  '--session-dir', str(mirror_root), '--port', str(port2),
                                  '--audio-port', str(audio_port2), '--token', bytes(16).hex()],
                                 cwd=ROOT, env=env, capture_output=True, text=True, timeout=8)
    assert wrong_token.returncode != 0 and 'rejected' in wrong_token.stderr
    move = subprocess.run([str(ROOT / 'kilix'), 'remote', 'retarget', '--name', 'audio-relay',
                           '--session-dir', str(mirror_root), '--port', str(port2),
                           '--audio-port', str(audio_port2), '--token', token2.hex()],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=8)
    assert move.returncode == 0, move.stderr
    deadline = time.monotonic() + 6
    while struct.pack('<h', -11000) * 100 not in pcm_path.read_bytes():
        assert viewer.poll() is None and time.monotonic() < deadline, 'Retargeted audio did not play'
        time.sleep(0.02)
    assert viewer.pid == first_viewer_pid and owner.poll() is None
finally:
    if viewer is not None:
        viewer.terminate()
        try:
            viewer.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            viewer.kill()
            viewer.communicate(timeout=3)
    for server in servers:
        if server.poll() is None:
            server.terminate()
        try:
            server.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate(timeout=3)
    owner.terminate()
    try:
        owner.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        owner.kill()
        owner.communicate(timeout=3)
    subprocess.run([str(ROOT / 'batty'), '--terminate', 'audio', '--session-dir', str(root)],
                   cwd=ROOT, env=env, capture_output=True, timeout=8, check=True)

assert not (mirror_root / 'audio-relay.sock').exists()
print('PASS authenticated PCM, direct audio client, native viewer output and audio retarget')
