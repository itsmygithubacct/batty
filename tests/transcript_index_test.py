#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Public transcript commands against live recording workers and hostile files."""
import io
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import kilix_transcript as transcripts


def command(root, *args, good=True):
    p = subprocess.run([str(ROOT / 'kilix'), 'transcript', '--directory', str(root), *args],
                       capture_output=True, timeout=5)
    assert (p.returncode == 0) == good, (p.returncode, p.stderr)
    return p.stdout


def wait_for(check):
    deadline = time.monotonic() + 5
    while not check():
        assert time.monotonic() < deadline, 'Writer deadline'
        time.sleep(0.005)


with tempfile.TemporaryDirectory(prefix='bt-transcript-index-') as directory:
    root = Path(directory)
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    parent.settimeout(5)
    args = ['/bin/echo', 'λ literal $(false)', 'line\n\x1b[31m', 'x' * 300]
    p = subprocess.Popen([str(ROOT / 'build/batty-transcript'), str(root), 'live', '32768', 'keep',
                          str(os.getpid()), '0', *args], stdin=child)
    child.close()
    try:
        parent.sendall(b'Drecorded output\r\n')
        wait_for(lambda: (root / 'live.log').exists() and (root / 'live.log').stat().st_size > 0)
        rows = json.loads(command(root, '--json'))
        assert len(rows) == 1 and rows[0]['state'] == 'recording'
        assert rows[0]['pid'] == os.getpid() and rows[0]['argv'][:3] == args[:3]
        assert rows[0]['truncated'] and rows[0]['argv'][3] == 'x' * 256
        assert rows[0]['cwd'] == str(ROOT)
        assert b'\x1b' not in command(root) and b'$(false)' in command(root)
        assert command(root, 'show', 'live') == b'recorded output\r\n'
        assert command(root, 'path', 'live').decode().strip() == str(root / 'live.log')
        assert command(root, 'path').decode().strip() == str(root)
        # Rotation replaces the log inode, but the metadata lock remains live.
        for _ in range(20):
            parent.sendall(b'D' + b'r' * 4096)
        parent.sendall(b'Drotated-end')
        wait_for(lambda: (root / 'live.log').read_bytes().endswith(b'rotated-end'))
        assert json.loads(command(root, '--json'))[0]['state'] == 'recording'
        parent.sendall(b'E')
        assert struct.unpack('<I', parent.recv(4))[0] == 0
        assert p.wait(timeout=5) == 0
        row = json.loads(command(root, '--json'))[0]
        assert row['state'] == 'complete' and row['error'] == 0 and row['ended'] >= row['started']
        assert command(root, 'show', 'live').endswith(b'rotated-end')
        assert (root / 'live.meta').stat().st_mode & 0o777 == 0o600
    finally:
        parent.close()
        if p.poll() is None:
            p.kill(); p.wait()

    # A killed writer has no completion record; no PID liveness guesses.
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    p = subprocess.Popen([str(ROOT / 'build/batty-transcript'), str(root), 'crashed', '32768', 'keep'], stdin=child)
    child.close()
    try:
        parent.sendall(b'Dbefore crash')
        wait_for(lambda: (root / 'crashed.log').exists() and (root / 'crashed.log').stat().st_size > 0)
        p.send_signal(signal.SIGKILL); p.wait(timeout=5)
        rows = {v['id']: v for v in json.loads(command(root, '--json'))}
        assert rows['crashed']['state'] == 'interrupted'
    finally:
        parent.close()
        if p.poll() is None:
            p.kill(); p.wait()

    # Older logs remain readable but do not acquire invented lifecycle metadata.
    legacy = root / 'legacy.log'
    legacy.write_bytes(b'old log'); legacy.chmod(0o600)
    assert next(v for v in json.loads(command(root, '--json')) if v['id'] == 'legacy')['state'] == 'unknown'
    assert command(root, 'show', 'legacy') == b'old log'
    fd = transcripts.open_root(str(root))
    try:
        class AppendDuringRead(io.BytesIO):
            def write(self, data):
                with legacy.open('ab') as stream:
                    stream.write(b'new output')
                return super().write(data)
        out = AppendDuringRead()
        transcripts.show(fd, 'legacy', out)
        assert out.getvalue() == b'old log'
    finally:
        os.close(fd)

    command(root, 'show', '../live', good=False)
    command(root, 'show', '.hidden', good=False)
    command(root, 'show', 'missing', good=False)
    (root / 'linked.log').symlink_to(legacy)
    command(root, 'show', 'linked', good=False)
    (root / 'linked.log').unlink()
    os.mkfifo(root / 'fifo.log', 0o600)
    command(root, 'show', 'fifo', good=False)
    (root / 'fifo.log').unlink()
    os.link(legacy, root / 'hard.log')
    command(root, 'show', 'hard', good=False)
    (root / 'hard.log').unlink()
    legacy.chmod(0o644)
    command(root, 'show', 'legacy', good=False)
    legacy.chmod(0o600)
    (root / 'live.meta').unlink()
    (root / 'live.meta').symlink_to(legacy)
    command(root, '--json', good=False)
    alias = root / 'alias'
    alias.symlink_to(root, target_is_directory=True)
    command(alias, 'path', good=False)
    root.chmod(0o755)
    command(root, 'path', good=False)
    root.chmod(0o700)
print('PASS transcript index: live locks, metadata, rotation, completion/crash, bounded reads and unsafe-file refusal')
