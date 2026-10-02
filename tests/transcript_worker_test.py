#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise the isolated transcript writer against real files and sockets."""
import errno
import os
from pathlib import Path
import resource
import socket
import struct
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent


def start(directory, name, limit=32768, policy='elide', file_limit=None, create=False, mask=None):
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    parent.settimeout(5)
    def restrict():
        if file_limit is not None:
            resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))
        if mask is not None:
            os.umask(mask)
    p = subprocess.Popen([str(ROOT / 'build/batty-transcript'), str(directory), name, str(limit), policy],
                         stdin=child, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         env=os.environ | {'BATTY_TRANSCRIPT_CREATE_ROOT': str(int(create))},
                         preexec_fn=restrict if file_limit is not None or mask is not None else None)
    child.close()
    return p, parent


def finish(p, channel, expected=0, clean=True):
    if clean:
        channel.sendall(b'E')
    else:
        channel.shutdown(socket.SHUT_WR)
    status = channel.recv(4)
    assert len(status) == 4 and struct.unpack('<I', status)[0] == expected, (status, expected)
    channel.close()
    assert p.wait(timeout=5) == int(expected != 0), p.stderr.read()
    p.stderr.close()


with tempfile.TemporaryDirectory(prefix='bt-transcript-') as directory:
    root = Path(directory)
    p, channel = start(root, 'graphics')
    data = b'hello\x1b_G' + b'A' * (4 * 1024 * 1024) + b'\x1b\\\nworld'
    for at in range(0, len(data), 12345):
        channel.sendall(b'D' + data[at:at + 12345])
    finish(p, channel)
    text = (root / 'graphics.log').read_bytes()
    assert text == b'hello\r\n[batty: 4194309 graphics bytes elided]\r\n\nworld', text
    assert (root / 'graphics.log').stat().st_mode & 0o777 == 0o600

    p, channel = start(root, 'c1-graphics')
    channel.sendall(b'Dbefore\xe2\x98')
    channel.sendall(b'D\x9f\x9fGabc')
    channel.sendall(b'D\x9cafter')
    finish(p, channel)
    assert (root / 'c1-graphics.log').read_bytes() == (
        b'before\xe2\x98\x9f\r\n[batty: 6 graphics bytes elided]\r\nafter')

    p, channel = start(root, 'rotate', limit=32768, policy='keep')
    data = b''.join(f'{i:08d}: terminal history\n'.encode() for i in range(50000))
    for at in range(0, len(data), 4096):
        channel.sendall(b'D' + data[at:at + 4096])
    finish(p, channel)
    tail = (root / 'rotate.log').read_bytes()
    assert 24576 <= len(tail) <= 32768 and data.endswith(tail)
    assert not list(root.glob('.*rotate-*'))

    p, channel = start(root, 'partial')
    channel.sendall(b'Dbefore\x1b_Gunfinished')
    finish(p, channel, errno.ECANCELED, clean=False)
    text = (root / 'partial.log').read_bytes()
    assert b'graphics bytes elided, incomplete' in text and b'transcript interrupted' in text
    assert b'unfinished' not in text

    p, channel = start(root, 'malformed')
    channel.sendall(b'bad protocol')
    status = channel.recv(4)
    assert struct.unpack('<I', status)[0] == errno.EPROTO
    channel.close(); assert p.wait(timeout=5) == 1; p.stderr.close()

    p, channel = start(root, 'oversized')
    channel.sendall(b'D' + b'x' * 70000)
    status = channel.recv(4)
    assert struct.unpack('<I', status)[0] == errno.EMSGSIZE
    channel.close(); assert p.wait(timeout=5) == 1; p.stderr.close()

    for name in ('graphics', 'symlink'):
        if name == 'symlink':
            (root / 'symlink.log').symlink_to(root / 'graphics.log')
        p, channel = start(root, name)
        status = channel.recv(4)
        assert struct.unpack('<I', status)[0] == errno.EEXIST
        channel.close(); assert p.wait(timeout=5) == 1; p.stderr.close()
    assert (root / 'graphics.log').read_bytes() == b'hello\r\n[batty: 4194309 graphics bytes elided]\r\n\nworld'
    assert not (root / 'symlink.meta').exists()
    (root / 'metadata-collision.meta').write_bytes(b'preserve existing metadata')
    p, channel = start(root, 'metadata-collision')
    assert struct.unpack('<I', channel.recv(4))[0] == errno.EEXIST
    channel.close(); assert p.wait(timeout=5) == 1; p.stderr.close()
    assert not (root / 'metadata-collision.log').exists()
    assert (root / 'metadata-collision.meta').read_bytes() == b'preserve existing metadata'

    p, channel = start(root, 'disk-error', limit=32768, policy='keep', file_limit=4096)
    channel.sendall(b'D' + b'x' * 8192)
    status = channel.recv(4)
    assert struct.unpack('<I', status)[0] == errno.EFBIG, status
    channel.close(); assert p.wait(timeout=5) == 1; p.stderr.close()
    assert (root / 'disk-error.log').stat().st_size <= 4096

    p, channel = start(root, 'rotation-error', limit=32768, policy='keep')
    initial = b'original history' * 2048
    assert len(initial) == 32768
    channel.sendall(b'D' + initial)
    path = root / 'rotation-error.log'
    deadline = time.monotonic() + 5
    while not path.exists() or path.stat().st_size != len(initial):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    resource.prlimit(p.pid, resource.RLIMIT_FSIZE, (4096, 4096))
    channel.sendall(b'D' + b'new' * 2048)
    status = channel.recv(4)
    assert struct.unpack('<I', status)[0] == errno.EFBIG
    channel.close(); assert p.wait(timeout=5) == 1; p.stderr.close()
    assert path.read_bytes() == initial and not list(root.glob('.*rotate-*'))

    p, channel = start(root, 'replaced', policy='keep')
    channel.sendall(b'Doriginal')
    path = root / 'replaced.log'
    deadline = time.monotonic() + 5
    while not path.exists() or path.stat().st_size != 8:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    path.unlink(); path.write_bytes(b'user replacement')
    channel.sendall(b'Dnew data')
    status = channel.recv(4)
    assert struct.unpack('<I', status)[0] == errno.ESTALE
    channel.close(); assert p.wait(timeout=5) == 1; p.stderr.close()
    assert path.read_bytes() == b'user replacement'

    p, channel = start(root, 'removed-final', policy='keep')
    channel.sendall(b'Doriginal')
    path = root / 'removed-final.log'
    deadline = time.monotonic() + 5
    while not path.exists() or path.stat().st_size != 8:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    path.unlink()
    finish(p, channel, errno.ENOENT)

    unsafe = root / 'unsafe'
    unsafe.mkdir(mode=0o755); unsafe.chmod(0o755)
    p, channel = start(unsafe, 'rejected')
    status = channel.recv(4)
    assert struct.unpack('<I', status)[0] == errno.EPERM
    channel.close(); assert p.wait(timeout=5) == 1; p.stderr.close()
    assert not list(unsafe.iterdir())

    nested = root / 'new-state' / 'batty' / 'transcripts'
    p, channel = start(nested, 'no-create')
    assert struct.unpack('<I', channel.recv(4))[0] == errno.ENOENT
    channel.close(); assert p.wait(timeout=5) == 1; p.stderr.close()
    assert not (root / 'new-state').exists()
    creators = [start(nested, name, create=True, mask=0o777) for name in ('first', 'second')]
    for p, channel in creators:
        finish(p, channel)
    for path in (root / 'new-state', nested.parent, nested):
        assert path.stat().st_mode & 0o777 == 0o700
    assert (nested / 'first.log').stat().st_mode & 0o777 == 0o600
    p, channel = start(str(root) + '/must-not-create/../elsewhere', 'traversal', create=True)
    assert struct.unpack('<I', channel.recv(4))[0] == errno.EINVAL
    channel.close(); assert p.wait(timeout=5) == 1; p.stderr.close()
    assert not (root / 'must-not-create').exists()
    (root / 'alias').symlink_to(nested, target_is_directory=True)
    p, channel = start(root / 'alias' / 'new', 'symlink-parent', create=True)
    assert struct.unpack('<I', channel.recv(4))[0] in (errno.ELOOP, errno.ENOTDIR)
    channel.close(); assert p.wait(timeout=5) == 1; p.stderr.close()
    assert not (nested / 'new').exists()
    p, channel = start(unsafe, 'still-unsafe', create=True)
    assert struct.unpack('<I', channel.recv(4))[0] == errno.EPERM
    channel.close(); assert p.wait(timeout=5) == 1; p.stderr.close()
    assert unsafe.stat().st_mode & 0o777 == 0o755 and not list(unsafe.iterdir())
    unsafe.chmod(0o1700)
    p, channel = start(unsafe, 'special-mode', create=True)
    assert struct.unpack('<I', channel.recv(4))[0] == errno.EPERM
    channel.close(); assert p.wait(timeout=5) == 1; p.stderr.close()
    assert unsafe.stat().st_mode & 0o7777 == 0o1700 and not list(unsafe.iterdir())

print('PASS transcript worker: private files, image elision, bounded atomic rotation, incomplete streams, protocol and disk failures')
