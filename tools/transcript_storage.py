#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Bounded compression and retention for inactive Batty transcripts."""
from contextlib import contextmanager
import ctypes
import errno
import fcntl
import hashlib
import os
import resource
import stat
import subprocess
import tempfile
import time
import uuid

MAX_LOG = 128 * 1024 * 1024
MAX_ENCODED = MAX_LOG + 1024 * 1024
# Set only by the single-threaded maintenance process while supervising codecs.
cancelled = lambda: False


class MaintenanceBusy(ValueError):
    pass


@contextmanager
def maintenance_lock(root, exclusive, name='.maintenance.lock'):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root)
    except FileExistsError:
        fd = os.open(name, flags, dir_fd=root)
        created = False
    else:
        created = True
    try:
        if created:
            os.fchmod(fd, 0o600)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size):
            raise ValueError('Unsafe transcript maintenance lock')
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MaintenanceBusy('Transcript maintenance is busy') from None
        path = os.stat(name, dir_fd=root, follow_symlinks=False)
        if (path.st_dev, path.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError('Transcript maintenance lock was replaced')
        yield
    finally:
        os.close(fd)


@contextmanager
def tier(root, name, create=False):
    if not name:
        yield root
        return
    if create:
        try:
            # The maintenance CLI and watcher are single-threaded processes.
            # Create the private tier even when the frontend inherited a more
            # restrictive umask; restore it immediately afterwards.
            previous = os.umask(0o077)
            try:
                os.mkdir(name, 0o700, dir_fd=root)
            finally:
                os.umask(previous)
            os.fsync(root)
        except FileExistsError:
            pass
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root)
    except FileNotFoundError:
        yield None
        return
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('Unsafe transcript storage tier')
        yield fd
    finally:
        os.close(fd)


def filename(identity, location):
    return identity + ('.log.zst' if location else '.log')


def run_codec(source, output, decode=False, level=3):
    def limits():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        _, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        cap = MAX_LOG if decode else MAX_ENCODED
        if hard != resource.RLIM_INFINITY:
            cap = min(cap, hard)
        resource.setrlimit(resource.RLIMIT_FSIZE, (cap, hard))
    os.lseek(source, 0, os.SEEK_SET)
    args = ['zstd', '-q', '-c', '--no-progress']
    args += ['-d', '--memory=128MB'] if decode else [f'-{level}', '-T1']
    if cancelled():
        raise InterruptedError('Transcript maintenance owner exited')
    process = subprocess.Popen(args, stdin=source, stdout=output, stderr=subprocess.PIPE, preexec_fn=limits)
    deadline = time.monotonic() + 30
    try:
        while True:
            if cancelled():
                raise InterruptedError('Transcript maintenance owner exited')
            if time.monotonic() >= deadline:
                raise ValueError('Transcript compression/decompression exceeded 30 seconds')
            try:
                _, diagnostic = process.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                pass
        if process.returncode:
            raise ValueError('Transcript compression/decompression failed: ' + diagnostic.decode(errors='replace').strip())
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    os.lseek(output, 0, os.SEEK_SET)


@contextmanager
def decoded(fd, compressed):
    if not compressed:
        yield fd
        return
    with tempfile.TemporaryFile() as plain:
        run_codec(fd, plain.fileno(), decode=True)
        yield plain.fileno()


def digest(fd):
    result = hashlib.sha256()
    length = os.fstat(fd).st_size
    if length > MAX_LOG:
        raise ValueError('Decoded transcript exceeds 128 MiB')
    at = 0
    while at < length:
        data = os.pread(fd, min(65536, length - at), at)
        if not data:
            raise ValueError('Transcript shortened during verification')
        result.update(data)
        at += len(data)
    return length, result.digest()


def unchanged(directory, name, fd, expected=None):
    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
    opened = expected or os.fstat(fd)
    if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
            opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
        raise ValueError('Transcript changed during maintenance')


def named_temporary_file(directory):
    name = '.compress-' + uuid.uuid4().hex
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=directory)
    return fd, name


def temporary_file(directory):
    try:
        # An unnamed inode vanishes on process death, even mid-compression.
        fd = os.open('.', os.O_RDWR | os.O_TMPFILE | os.O_CLOEXEC, 0o600, dir_fd=directory)
        return fd, None
    except OSError as error:
        if error.errno not in (errno.EOPNOTSUPP, errno.EINVAL, errno.EISDIR):
            raise
    return named_temporary_file(directory)


def publish(directory, temporary, name, fd):
    libc = ctypes.CDLL(None, use_errno=True)
    if temporary is None:
        # linkat(AT_EMPTY_PATH) publishes O_TMPFILE without an intermediate name.
        link = libc.linkat
        link.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        link.restype = ctypes.c_int
        result = link(fd, b'', directory, os.fsencode(name), 0x1000)
    else:
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(directory, os.fsencode(temporary), directory, os.fsencode(name), 1)
    if result:
        error = ctypes.get_errno()
        if temporary is None and error in (errno.ENOENT, errno.EOPNOTSUPP):
            # Some filesystems create O_TMPFILE inodes but cannot link them.
            # Copy the already verified bytes to a private named inode, then
            # publish it with the same no-replace rename used above.
            copy, copy_name = named_temporary_file(directory)
            try:
                os.fchmod(copy, 0o600)
                size = os.fstat(fd).st_size
                offset = 0
                while offset < size:
                    block = os.pread(fd, min(1024 * 1024, size - offset), offset)
                    if not block:
                        raise OSError(errno.EIO, 'Transcript shortened during publication')
                    view = memoryview(block)
                    while view:
                        written = os.write(copy, view)
                        if not written:
                            raise OSError(errno.EIO, 'Transcript copy made no progress')
                        view = view[written:]
                    offset += len(block)
                os.fsync(copy)
                publish(directory, copy_name, name, copy)
                return
            finally:
                os.close(copy)
                try:
                    os.unlink(copy_name, dir_fd=directory)
                except FileNotFoundError:
                    pass
        raise OSError(error, os.strerror(error), name)


@contextmanager
def inactive(root, identity, api):
    try:
        fd, _ = api.open_file(root, identity + '.meta', api.MAX_META)
    except FileNotFoundError:
        yield None
        return
    try:
        try:
            # Shared locks test the writer's exclusive lock without making a
            # concurrent index mistake our maintenance for a live writer.
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        info = api.metadata(root, identity)
        unchanged(root, identity + '.meta', fd)
        if info['state'] in ('unknown', 'recording'):
            yield None
        else:
            yield fd
    finally:
        os.close(fd)


def transfer(root, identity, origin, destination, api):
    with inactive(root, identity, api) as meta:
        if meta is None:
            return False
        with tier(root, origin) as source_dir, tier(root, destination, True) as target_dir:
            source_name, target_name = filename(identity, origin), filename(identity, destination)
            source, source_info = api.open_file(source_dir, source_name, MAX_ENCODED if origin else MAX_LOG)
            temporary = None
            target = -1
            try:
                with decoded(source, bool(origin)) as plain:
                    expected = digest(plain)
                    try:
                        existing, _ = api.open_file(target_dir, target_name, MAX_ENCODED)
                    except FileNotFoundError:
                        existing = None
                    if existing is not None:
                        try:
                            with decoded(existing, True) as check:
                                if digest(check) != expected:
                                    raise ValueError('Conflicting transcript copies; preserving both')
                            unchanged(target_dir, target_name, existing)
                        finally:
                            os.close(existing)
                    else:
                        target, temporary = temporary_file(target_dir)
                        os.fchmod(target, 0o600)
                        run_codec(plain, target, level=9 if destination == 'archive' else 3)
                        # Verify decompression before publishing or deleting the source.
                        with decoded(target, True) as check:
                            if digest(check) != expected:
                                raise ValueError('Transcript compression verification failed')
                        os.fsync(target)
                        publish(target_dir, temporary, target_name, target)
                        os.fsync(target_dir)
                    unchanged(root, identity + '.meta', meta)
                    unchanged(source_dir, source_name, source, source_info)
                    os.unlink(source_name, dir_fd=source_dir)
                    os.fsync(source_dir)
                    return True
            finally:
                if target >= 0:
                    os.close(target)
                    if temporary is not None:
                        try:
                            os.unlink(temporary, dir_fd=target_dir)
                        except FileNotFoundError:
                            pass
                os.close(source)


def remove(root, identity, location, api):
    with inactive(root, identity, api) as meta:
        if meta is None:
            return False
        with tier(root, location) as directory:
            name = filename(identity, location)
            fd, _ = api.open_file(directory, name, MAX_ENCODED)
            try:
                unchanged(root, identity + '.meta', meta)
                unchanged(directory, name, fd)
                os.unlink(name, dir_fd=directory)
                os.fsync(directory)
            finally:
                os.close(fd)
        # Keep metadata while any other recoverable copy exists.
        for place in ('', 'recent', 'archive'):
            with tier(root, place) as directory:
                if directory is not None:
                    try:
                        os.stat(filename(identity, place), dir_fd=directory, follow_symlinks=False)
                        return True
                    except FileNotFoundError:
                        pass
        unchanged(root, identity + '.meta', meta)
        os.unlink(identity + '.meta', dir_fd=root)
        os.fsync(root)
        return True


def maintain(root, api, recent_budget, archive_budget, archive_all=False,
             max_operations=None, max_seconds=None):
    if archive_all and not archive_budget:
        raise ValueError('archive requires a nonzero archive budget')
    if max_operations is not None and max_operations < 1:
        raise ValueError('Maintenance operation limit must be positive')
    if max_seconds is not None and max_seconds <= 0:
        raise ValueError('Maintenance time budget must be positive')
    result = dict(compressed=0, archived=0, removed=0, protected=0, deferred=False)
    protected = set()
    operations = 0
    started = time.monotonic()

    def action(row, origin, destination=None):
        nonlocal operations
        if row['state'] in ('recording', 'unknown'):
            protected.add(row['id'])
            return False
        if cancelled():
            raise InterruptedError('Transcript maintenance owner exited')
        if ((max_operations is not None and operations >= max_operations)
                or (max_seconds is not None and time.monotonic() - started >= max_seconds)):
            result['deferred'] = True
            return False
        operations += 1
        moved = (transfer(root, row['id'], origin, destination, api) if destination
                 else remove(root, row['id'], origin, api))
        if not moved:
            protected.add(row['id'])
        else:
            result['compressed' if destination == 'recent' else 'archived' if destination else 'removed'] += 1
        return moved

    def trim_archive():
        rows = [r for r in reversed(api.index(root)) if 'archive' in r['copies']]
        total = sum(r['sizes']['archive'] for r in rows)
        for row in rows:
            if total <= archive_budget or result['deferred']:
                break
            if action(row, 'archive'):
                total -= row['sizes']['archive']

    def trim_recent():
        rows = [r for r in reversed(api.index(root)) if 'recent' in r['copies']]
        total = sum(r['sizes']['recent'] for r in rows)
        for row in rows:
            if (total <= recent_budget and not archive_all) or result['deferred']:
                break
            if action(row, 'recent', 'archive' if archive_budget else None):
                total -= row['sizes']['recent']

    # Enforce existing budgets before adding compressed files. Bounded passes
    # must not perpetually postpone eviction while new raw logs keep arriving.
    trim_archive()
    trim_recent()
    trim_archive()
    for row in api.index(root):
        if result['deferred']:
            break
        if 'recent' in row['copies'] and 'archive' in row['copies']:
            action(row, 'recent', 'archive')
    for row in reversed(api.index(root)):
        if result['deferred']:
            break
        if '' in row['copies']:
            action(row, '', 'recent')
    trim_recent()
    trim_archive()
    rows = api.index(root)
    result['protected'] = len(protected)
    result['operations'] = operations
    result['recent_bytes'] = sum(r['sizes'].get('recent', 0) for r in rows)
    result['archive_bytes'] = sum(r['sizes'].get('archive', 0) for r in rows)
    result['over_budget'] = result['recent_bytes'] > recent_budget or result['archive_bytes'] > archive_budget
    return result
