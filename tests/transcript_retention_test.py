#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Lossless tiered retention, live exclusion and failed-transaction recovery."""
import errno
import io
import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import kilix_transcript as api
import transcript_storage as storage


def cli(root, *args, good=True):
    result = subprocess.run([str(ROOT / 'kilix'), 'transcript', '--directory', str(root), *args],
                            capture_output=True, timeout=15)
    assert (result.returncode == 0) == good, (result.returncode, result.stderr)
    return result.stdout


def worker(root, identity, data, live=False, started=1):
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    parent.settimeout(5)
    process = subprocess.Popen([str(ROOT / 'build/batty-transcript'), str(root), identity, '131072', 'keep'], stdin=child)
    child.close()
    for at in range(0, len(data), 8192):
        parent.sendall(b'D' + data[at:at + 8192])
    if live:
        deadline = time.monotonic() + 5
        while not (root / (identity + '.log')).exists() or (root / (identity + '.log')).stat().st_size != len(data):
            assert time.monotonic() < deadline
            time.sleep(0.005)
        return process, parent
    parent.sendall(b'E')
    assert struct.unpack('<I', parent.recv(4))[0] == 0
    parent.close(); assert process.wait(timeout=5) == 0
    meta = root / (identity + '.meta')
    records = [json.loads(line) for line in meta.read_text().splitlines()]
    records[0]['started'] = started
    meta.write_text(''.join(json.dumps(record) + '\n' for record in records))


with tempfile.TemporaryDirectory(prefix='bt-retention-') as directory:
    root = Path(directory)
    data = {name: os.urandom(32768) for name in ('old', 'middle', 'new')}
    for age, (name, content) in enumerate(data.items()):
        worker(root, name, content, started=age + 1)
    live, channel = worker(root, 'active', b'still recording', live=True)
    original = (root / 'active.log').stat().st_ino
    try:
        report = json.loads(cli(root, 'prune', '--recent-budget', '40000', '--archive-budget', '40000'))
        assert report['compressed'] == 3 and report['archived'] == 2 and report['removed'] == 1, report
        assert not report['over_budget'] and report['recent_bytes'] <= 40000 and report['archive_bytes'] <= 40000
        assert not (root / 'old.meta').exists()
        rows = {v['id']: v for v in json.loads(cli(root, '--json'))}
        assert set(rows) == {'active', 'middle', 'new'}
        assert rows['new']['tier'] == 'recent' and rows['middle']['tier'] == 'archive'
        for name in ('middle', 'new'):
            assert cli(root, 'show', name) == data[name]
            assert cli(root, 'path', name).decode().strip().endswith(name + '.log.zst')
        assert (root / 'active.log').stat().st_ino == original
        assert (root / 'active.log').read_bytes() == b'still recording'
        assert rows['active']['state'] == 'recording'
        report = json.loads(cli(root, 'archive', '--archive-budget', '1M'))
        assert report['archived'] == 1
        assert cli(root, 'show', 'new') == data['new']
        # Old unindexed logs cannot be guessed inactive and deleted.
        (root / 'legacy.log').write_bytes(b'preserve'); (root / 'legacy.log').chmod(0o600)
        report = json.loads(cli(root, 'prune', '--recent-budget', 'off', '--archive-budget', 'off'))
        assert report['removed'] == 2 and report['protected'] == 2
        assert (root / 'legacy.log').read_bytes() == b'preserve'
        assert (root / 'active.log').stat().st_ino == original
        cli(root, 'archive', '--archive-budget', 'off', good=False)
    finally:
        channel.close()
        if live.poll() is None:
            live.wait(timeout=5)

with tempfile.TemporaryDirectory(prefix='bt-retention-failure-') as directory:
    root = Path(directory)
    worker(root, 'source', b'lossless\n' * 8000)
    fd = api.open_root(str(root))
    try:
        before = (root / 'source.log').read_bytes()
        with storage.maintenance_lock(fd, True):
            def fail_codec(source, target, **kwargs):
                os.write(target, b'incomplete')
                raise OSError(errno.ENOSPC, 'injected full disk')
            with patch.object(storage, 'run_codec', fail_codec):
                try: storage.transfer(fd, 'source', '', 'recent', api)
                except OSError as error: assert error.errno == errno.ENOSPC
                else: raise AssertionError('Expected disk failure')
            assert (root / 'source.log').read_bytes() == before
            assert not list((root / 'recent').iterdir())
            with patch.object(storage.subprocess, 'Popen', side_effect=FileNotFoundError('missing codec')):
                try: storage.transfer(fd, 'source', '', 'recent', api)
                except FileNotFoundError: pass
                else: raise AssertionError('Expected missing codec')
            assert (root / 'source.log').read_bytes() == before
            # Simulate interruption after durable publication, before source unlink.
            unlink = os.unlink
            def interrupt(name, **kwargs):
                if name == 'source.log':
                    raise OSError(errno.EIO, 'injected interruption')
                return unlink(name, **kwargs)
            with patch.object(storage.os, 'unlink', interrupt):
                try: storage.transfer(fd, 'source', '', 'recent', api)
                except OSError as error: assert error.errno == errno.EIO
                else: raise AssertionError('Expected interrupted transfer')
            assert (root / 'source.log').read_bytes() == before
            assert (root / 'recent/source.log.zst').is_file()
            assert storage.transfer(fd, 'source', '', 'recent', api)
            assert not (root / 'source.log').exists()
        assert cli(root, 'show', 'source') == before
        worker(root, 'resume', b'recover archive transition')
        with storage.maintenance_lock(fd, True):
            assert storage.transfer(fd, 'resume', '', 'recent', api)
            def interrupt_archive(name, **kwargs):
                if name == 'resume.log.zst':
                    raise OSError(errno.EIO, 'injected archive interruption')
                return unlink(name, **kwargs)
            with patch.object(storage.os, 'unlink', interrupt_archive):
                try: storage.transfer(fd, 'resume', 'recent', 'archive', api)
                except OSError as error: assert error.errno == errno.EIO
                else: raise AssertionError('Expected archive interruption')
        assert (root / 'recent/resume.log.zst').exists() and (root / 'archive/resume.log.zst').exists()
        cli(root, 'prune', '--recent-budget', '1M', '--archive-budget', '1M')
        assert not (root / 'recent/resume.log.zst').exists()
        assert cli(root, 'show', 'resume') == b'recover archive transition'
        # Readers fail promptly while maintenance owns the directory transaction.
        with storage.maintenance_lock(fd, True):
            cli(root, '--json', good=False)
        # Output limits are applied in the decoder child, not to this process.
        with patch.object(storage, 'MAX_LOG', 4096):
            out = io.BytesIO()
            try: api.show(fd, 'source', out)
            except ValueError: pass
            else: raise AssertionError('Expected bounded decoder failure')
            assert not out.getvalue()
        # A duplicate destination with different content must preserve both.
        (root / 'source.log').write_bytes(b'new unrelated bytes'); (root / 'source.log').chmod(0o600)
        compressed = (root / 'recent/source.log.zst').read_bytes()
        with storage.maintenance_lock(fd, True):
            try: storage.transfer(fd, 'source', '', 'recent', api)
            except ValueError as error: assert 'Conflicting' in str(error)
            else: raise AssertionError('Expected collision refusal')
        assert (root / 'source.log').read_bytes() == b'new unrelated bytes'
        assert (root / 'recent/source.log.zst').read_bytes() == compressed
        (root / 'source.log').unlink()
        (root / 'recent/source.log.zst').write_bytes(compressed[:-5])
        assert not cli(root, 'show', 'source', good=False)
    finally:
        os.close(fd)

with tempfile.TemporaryDirectory(prefix='bt-retention-unsafe-') as directory:
    root = Path(directory)
    worker(root, 'safe', b'preserve')
    (root / 'recent').symlink_to(root, target_is_directory=True)
    cli(root, 'prune', good=False)
    assert (root / 'safe.log').read_bytes() == b'preserve'

with tempfile.TemporaryDirectory(prefix='bt-retention-umask-') as directory:
    root = Path(directory)
    content = b'private transcript under restrictive umask\n' * 100
    worker(root, 'private', content)
    result = subprocess.run([str(ROOT / 'kilix'), 'transcript', '--directory', str(root),
                             'prune', '--recent-budget', '1M', '--archive-budget', '1M'],
                            capture_output=True, timeout=15,
                            preexec_fn=lambda: os.umask(0o777))
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE((root / '.maintenance.lock').stat().st_mode) == 0o600
    assert stat.S_IMODE((root / 'recent').stat().st_mode) == 0o700
    assert cli(root, 'show', 'private') == content

with tempfile.TemporaryDirectory(prefix='bt-retention-crash-') as directory:
    root = Path(directory)
    content = b'preserve source after maintenance death\n' * 200
    worker(root, 'crashed', content)
    (root / 'recent').mkdir(mode=0o700)
    try:
        probe = os.open(root / 'recent', os.O_RDWR | os.O_TMPFILE | os.O_CLOEXEC, 0o600)
    except OSError as error:
        assert error.errno in (errno.EOPNOTSUPP, errno.EINVAL, errno.EISDIR), error
        unnamed = False
    else:
        os.close(probe)
        unnamed = True
    script = '''import os, signal, sys
import kilix_transcript as api
import transcript_storage as storage
root = api.open_root(sys.argv[1])
def die(source, output, **kwargs):
    os.write(output, b'incomplete compression')
    os.fsync(output)
    os.kill(os.getpid(), signal.SIGKILL)
storage.run_codec = die
with storage.maintenance_lock(root, True):
    storage.transfer(root, 'crashed', '', 'recent', api)
'''
    result = subprocess.run([sys.executable, '-c', script, str(root)],
                            env=os.environ | {'PYTHONPATH': str(ROOT / 'tools')},
                            capture_output=True, timeout=5)
    assert result.returncode == -signal.SIGKILL, (result.returncode, result.stderr)
    assert (root / 'crashed.log').read_bytes() == content
    assert not (root / 'recent/crashed.log.zst').exists()
    if unnamed:
        assert not list((root / 'recent').iterdir())
    assert json.loads(cli(root, 'prune', '--recent-budget', '1M', '--archive-budget', '1M'))['compressed'] == 1
    assert cli(root, 'show', 'crashed') == content

with tempfile.TemporaryDirectory(prefix='bt-retention-fallback-') as directory:
    root = Path(directory)
    content = b'fallback remains recoverable\n' * 100
    worker(root, 'fallback', content)
    fd = api.open_root(str(root))
    original_open = os.open
    def unsupported_tmpfile(path, flags, *args, **kwargs):
        if path == '.' and flags & os.O_TMPFILE == os.O_TMPFILE:
            raise OSError(errno.EOPNOTSUPP, 'unnamed files unsupported')
        return original_open(path, flags, *args, **kwargs)
    try:
        with patch.object(storage.os, 'open', unsupported_tmpfile):
            with storage.maintenance_lock(fd, True):
                assert storage.transfer(fd, 'fallback', '', 'recent', api)
    finally:
        os.close(fd)
    assert cli(root, 'show', 'fallback') == content
    assert not list((root / 'recent').glob('.compress-*'))
print('PASS transcript retention: live exclusion, lossless tiers, oldest-first budgets, transaction recovery and failure isolation')
