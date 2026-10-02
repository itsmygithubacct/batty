#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""One bounded semantic input peer for a persistent Batty terminal owner."""
from array import array
import errno
import fcntl
import os
import socket
import stat
import struct

from kilix_frame_source import FrameSource, HEADER, MAGIC, SEALS, VERSION, packet, receive

SEND = 3
INTENT = 4
RESIZE = 5
TEXT = 6
CLIPBOARD = 10
INTENT_LAYOUT = struct.Struct('<8I3iI')
MAX_INPUT = 4 * 1024 * 1024


class InputSource(FrameSource):
    def __init__(self, root, name, expected_epoch=0, *, read_only=False):
        super().__init__(root, name, expected_epoch, _role=2 if read_only else 3)

    def _operation(self, kind, payload=b'', intent=None, geometry=None):
        if len(payload) > MAX_INPUT:
            raise ValueError('Semantic input exceeds 4 MiB')
        if payload and kind not in (SEND, INTENT):
            raise ValueError('Unexpected semantic input payload')
        descriptor = -1
        if payload:
            descriptor = os.memfd_create('batty-remote-input', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            try:
                view = memoryview(payload)
                while view:
                    view = view[os.write(descriptor, view):]
                fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, SEALS)
            except BaseException:
                os.close(descriptor)
                raise
        try:
            self.number += 1
            words = [0] * 23
            words[0], words[2], words[6] = self.number, self.epoch, len(payload)
            words[7:11] = MAGIC, VERSION, kind, 0
            if geometry:
                words[12:16] = geometry
            if intent is not None:
                if len(intent) != INTENT_LAYOUT.size:
                    raise ValueError('Invalid semantic input intent')
                words[21] = len(intent)
            ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                          array('i', [descriptor]).tobytes())] if descriptor >= 0 else []
            self.sock.sendmsg([HEADER.pack(*words) + (intent or b'')], ancillary)
            reply, received = receive(self.sock, self.number, kind)
            if received >= 0:
                os.close(received)
                raise RuntimeError('Semantic input response contained a descriptor')
            if reply[2] != self.epoch:
                raise RuntimeError('Batty owner changed during input')
            self.latest_words = reply
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def send(self, data):
        self._operation(SEND, bytes(data))

    def intent(self, fields, text=b''):
        if len(fields) != 12:
            raise ValueError('Semantic input intent needs 12 fields')
        if fields[0] == 5:
            raise ValueError('Remote input cannot change focus')
        self._operation(INTENT, bytes(text), INTENT_LAYOUT.pack(*fields))

    def resize(self, cols, rows, cell_width, cell_height):
        if not (1 <= cols <= 1000 and 1 <= rows <= 1000 and
                1 <= cell_width <= 512 and 1 <= cell_height <= 512):
            raise ValueError('Invalid remote terminal geometry')
        self._operation(RESIZE, geometry=(cols, rows, cell_width, cell_height))

    def text(self, selection=False):
        self.number += 1
        self.sock.sendall(packet(self.number, TEXT, flags=int(bool(selection)), epoch=self.epoch))
        reply, descriptor = receive(self.sock, self.number, TEXT)
        if reply[2] != self.epoch:
            if descriptor >= 0:
                os.close(descriptor)
            raise RuntimeError('Batty owner changed during text retrieval')
        size = reply[6]
        if size > 1024 * 1024:
            if descriptor >= 0:
                os.close(descriptor)
            raise OSError(errno.E2BIG, 'Batty text response exceeds the remote limit')
        if (size and descriptor < 0) or (not size and descriptor >= 0):
            if descriptor >= 0:
                os.close(descriptor)
            raise ValueError('Invalid Batty text response')
        if not size:
            return b''
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_size != size or
                    fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & SEALS != SEALS):
                raise ValueError('Unsafe Batty text response')
            data = os.pread(descriptor, size, 0)
            if len(data) != size:
                raise ValueError('Short Batty text response')
            return data
        finally:
            os.close(descriptor)

    def clipboard(self):
        self.number += 1
        self.sock.sendall(packet(self.number, CLIPBOARD, epoch=self.epoch))
        reply, descriptor = receive(self.sock, self.number, CLIPBOARD)
        if reply[2] != self.epoch:
            if descriptor >= 0:
                os.close(descriptor)
            raise RuntimeError('Batty owner changed during clipboard retrieval')
        size = reply[6]
        if size > 1024 * 1024 + 1:
            if descriptor >= 0:
                os.close(descriptor)
            raise OSError(errno.E2BIG, 'Batty clipboard exceeds the remote limit')
        if not size and descriptor < 0:
            return b''
        if not size or descriptor < 0:
            if descriptor >= 0:
                os.close(descriptor)
            raise ValueError('Invalid Batty clipboard response')
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_size != size or
                    fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & SEALS != SEALS):
                raise ValueError('Unsafe Batty clipboard response')
            data = os.pread(descriptor, size, 0)
            if len(data) != size or data[-1:] != b'\0' or b'\0' in data[:-1]:
                raise ValueError('Invalid Batty clipboard text')
            return data
        finally:
            os.close(descriptor)
