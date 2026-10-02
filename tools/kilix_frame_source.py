#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Read sealed semantic frames from a Batty persistent-session observer."""
from array import array
import fcntl
import os
from pathlib import Path
import re
import socket
import stat
import struct

HEADER = struct.Struct('<7Q16I')
MAGIC = 0x42545953
VERSION = 1
HELLO = 1
FRAME = 2
CHUNK = 16384
FRAME_LIMIT = 128 * 1024 * 1024
SEALS = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
NAME = re.compile(r'[A-Za-z0-9_\-.]{1,48}\Z')


class Frame:
    def __init__(self, descriptor, words):
        self.fd = descriptor
        self.revision, self.epoch, self.size = words[1], words[2], words[6]
        self.columns, self.rows = words[12], words[13]
        self.cell_width, self.cell_height = words[14], words[15]
        self.child, self.exit_status, self.state = words[16:19]
        self.bytes_read, self.bytes_written, self.pending = words[3:6]

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def packet(number, kind, *, flags=0, epoch=0, revision=0):
    words = [0] * 23
    words[0] = number
    words[1] = revision
    words[2] = epoch
    words[7:11] = [MAGIC, VERSION, kind, flags]
    return HEADER.pack(*words)


def receive(sock, number, kind):
    raw, control, message_flags, _ = sock.recvmsg(HEADER.size + CHUNK,
                                                   socket.CMSG_SPACE(8 * array('i').itemsize),
                                                   socket.MSG_CMSG_CLOEXEC)
    descriptors = []
    try:
        for level, control_type, payload in control:
            if level != socket.SOL_SOCKET or control_type != socket.SCM_RIGHTS or len(payload) % 4:
                raise RuntimeError('Malformed Batty descriptor response')
            received = array('i')
            received.frombytes(payload)
            descriptors.extend(received)
        if message_flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(descriptors) > 1:
            raise RuntimeError('Truncated Batty observer response')
        if len(raw) < HEADER.size:
            raise RuntimeError('Batty observer disconnected')
        words = HEADER.unpack(raw[:HEADER.size])
        if (words[0] != number or words[7:10] != (MAGIC, VERSION, kind) or
                words[22] or words[21] > CHUNK or len(raw) != HEADER.size + words[21]):
            raise RuntimeError('Invalid Batty observer response')
        if words[11]:
            raise OSError(words[11], os.strerror(words[11]))
        descriptor = descriptors.pop() if descriptors else -1
        return words, descriptor
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def validate_frame(descriptor, words):
    size = words[6]
    if descriptor < 0:
        if size:
            raise RuntimeError('Missing Batty semantic frame')
        return None
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or not 860 <= size <= FRAME_LIMIT or
                info.st_size != size or fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & SEALS != SEALS):
            raise RuntimeError('Unsafe Batty semantic frame')
        head = os.pread(descriptor, 32, 0)
        if len(head) != 32 or head[:8] != b'BTPRES01':
            raise RuntimeError('Invalid Batty semantic frame header')
        version, encoded_size, epoch, revision = struct.unpack_from('<IIQQ', head, 8)
        if (version != 1 or encoded_size != size or epoch != words[2] or
                revision != words[1] or not epoch or not revision):
            raise RuntimeError('Batty semantic frame metadata disagrees with its owner')
        geometry = struct.unpack_from('<IIII', os.pread(descriptor, 48, 0), 32)
        if geometry != (words[12], words[13], words[14], words[15]):
            raise RuntimeError('Batty semantic frame geometry disagrees with its owner')
        return Frame(descriptor, words)
    except BaseException:
        os.close(descriptor)
        raise


class FrameSource:
    """One bounded read-only observer slot in a named persistent PTY owner."""

    def __init__(self, root, name, expected_epoch=0, *, _role=2):
        if not NAME.fullmatch(name) or name.startswith('.'):
            raise ValueError('Invalid Batty session name')
        if not isinstance(expected_epoch, int) or expected_epoch < 0 or expected_epoch > 0xffffffffffffffff:
            raise ValueError('Invalid Batty owner epoch')
        if _role not in (2, 3):
            raise ValueError('Invalid Batty observer role')
        directory = Path(root)
        if not directory.is_absolute():
            raise ValueError('Batty session root must be absolute')
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o777 != 0o700:
            raise PermissionError('Batty session root is not private')
        endpoint = directory / f'{name}.sock'
        info = endpoint.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o777 != 0o600:
            raise PermissionError('Batty session endpoint is not private')
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.sock.settimeout(3.5)
        try:
            self.sock.connect(str(endpoint))
            credentials = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if struct.unpack('3i', credentials)[1] != os.geteuid():
                raise PermissionError('Batty owner has a different UID')
            self.number = 1
            self.sock.sendall(packet(1, HELLO, flags=_role, epoch=expected_epoch))
            words, descriptor = receive(self.sock, 1, HELLO)
            if expected_epoch and words[2] != expected_epoch:
                if descriptor >= 0:
                    os.close(descriptor)
                raise RuntimeError('Batty owner epoch changed')
            self.latest_words = words
            self.frame = validate_frame(descriptor, words)
            if self.frame is None:
                raise RuntimeError('Batty observer did not supply its initial frame')
            self.epoch = self.frame.epoch
            self.revision = self.frame.revision
        except BaseException:
            self.sock.close()
            raise

    def poll(self, timeout_ms=100):
        if not 0 <= timeout_ms <= 100:
            raise ValueError('Frame poll timeout must be 0..100 ms')
        self.number += 1
        self.sock.sendall(packet(self.number, FRAME, flags=timeout_ms,
                                 epoch=self.epoch, revision=self.revision))
        words, descriptor = receive(self.sock, self.number, FRAME)
        self.latest_words = words
        frame = validate_frame(descriptor, words)
        if frame is not None:
            if frame.epoch != self.epoch or frame.revision <= self.revision:
                frame.close()
                raise RuntimeError('Batty owner frame revision did not advance')
            self.revision = frame.revision
        return frame

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        if self.frame is not None:
            self.frame.close()
            self.frame = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
