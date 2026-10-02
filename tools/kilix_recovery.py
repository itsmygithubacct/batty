#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Private durable output archives for replacing a lost Batty owner."""
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import struct
import tempfile

from kilix_frame_source import FrameSource, FRAME_LIMIT, SEALS, packet, receive

HEADER = struct.Struct('<8s10I')
RECOVERY = 12


def private_directory(path, create=False):
    path = Path(path)
    if create:
        # Helpers and the CLI run in their own single-threaded process. Create
        # usable private directories even with a caller's restrictive umask.
        previous = os.umask(0o077)
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        finally:
            os.umask(previous)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o777 != 0o700:
        raise ValueError('Recovery directory must be private and user-owned')
    return path


@contextmanager
def directory_lock(path):
    """Coordinate manifest publication, archive pruning and snapshot removal."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield descriptor
    finally:
        os.close(descriptor)


def recovery_directory(create=False):
    return private_directory(os.environ.get('BATTY_KILIX_RECOVERY_DIR') or
                             Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) /
                             'batty/recovery', create)


def decode(data):
    if not HEADER.size <= len(data) <= FRAME_LIMIT:
        raise ValueError('Invalid output archive size')
    magic, version, total, vt, frame, args, cwd, cols, rows, cw, ch = HEADER.unpack_from(data)
    if (magic != b'BTRCV001' or version != 1 or total != len(data) or
            HEADER.size + vt + frame + args + cwd != total or not 0 < args <= 65536 or cwd >= 4096 or
            not 0 < cols <= 1000 or not 0 < rows <= 1000 or not 0 < cw <= 512 or not 0 < ch <= 512):
        raise ValueError('Invalid output archive header')
    start = HEADER.size + vt
    scene = data[start:start + frame]
    if (len(scene) < 860 or scene[:8] != b'BTPRES01' or
            struct.unpack_from('<II', scene, 8) != (1, frame) or
            struct.unpack_from('<IIII', scene, 32) != (cols, rows, cw, ch)):
        raise ValueError('Invalid output archive frame')
    epoch = struct.unpack_from('<Q', scene, 16)[0]
    start += frame
    command = data[start:start + args]
    if not command.endswith(b'\0'):
        raise ValueError('Invalid saved program arguments')
    command = command[:-1].decode('utf-8').split('\0')
    if not command[0]:
        raise ValueError('Missing saved program')
    directory = data[start + args:].decode('utf-8')
    if '\0' in directory or directory and not directory.startswith('/'):
        raise ValueError('Invalid saved working directory')
    return {'epoch': f'{epoch:016x}', 'argv': command, 'cwd': directory}


def capture(pane):
    with FrameSource(pane['session_dir'], pane['session'], int(pane['session_epoch'], 16)) as source:
        source.number += 1
        source.sock.sendall(packet(source.number, RECOVERY, epoch=source.epoch))
        words, descriptor = receive(source.sock, source.number, RECOVERY)
        if descriptor < 0:
            raise RuntimeError('Owner did not supply recovery output')
        try:
            size = words[6]
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or not HEADER.size <= size <= FRAME_LIMIT or
                    info.st_size != size or fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & SEALS != SEALS):
                raise ValueError('Unsafe recovery output')
            with os.fdopen(os.dup(descriptor), 'rb') as stream:
                stream.seek(0)
                data = stream.read(FRAME_LIMIT + 1)
            metadata = decode(data)
            if metadata['epoch'] != pane['session_epoch']:
                raise ValueError('Recovery output epoch changed')
            # Archive frames are static. Normalize the publication revision so
            # unchanged output reuses one file across periodic captures.
            data = bytearray(data)
            frame_start = HEADER.size + struct.unpack_from('<I', data, 16)[0]
            struct.pack_into('<Q', data, frame_start + 24, 1)
            return bytes(data)
        finally:
            os.close(descriptor)


def write(directory, data):
    name = hashlib.sha256(data).hexdigest() + '.bt-output'
    target = directory / name
    if target.exists():
        # Never trust an existing hash-named file without checking its content.
        read(target, name[:64])
        return name
    fd, temporary = tempfile.mkstemp(prefix='.output-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        root = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(root)
        finally:
            os.close(root)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return name


def read(path, expected_hash):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                info.st_mode & 0o077 or not HEADER.size <= info.st_size <= FRAME_LIMIT):
            raise ValueError('Unsafe output archive')
        data = stream.read(FRAME_LIMIT + 1)
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise ValueError('Output archive checksum mismatch')
    return decode(data)


def owner_missing(pane):
    try:
        with FrameSource(pane['session_dir'], pane['session'], int(pane['session_epoch'], 16)):
            return False
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ECONNREFUSED, errno.ECONNRESET):
            return True
        raise  # A live replacement, busy owner or timeout cannot authorize recreation.
