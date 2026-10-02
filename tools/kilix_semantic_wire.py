#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Bounded network framing for Batty's portable semantic presentation codec."""
import hmac
import os
import socket
import struct
import time
import zlib

from kilix_frame_source import FRAME_LIMIT

AUTH_REQUEST = b'BTR1'
AUTH_ACCEPT = b'BTA1'
AUTH_REJECT = b'BTE1'
FRAME_MAGIC = b'BTF2'
COMPRESSED_MAGIC = b'BTZ2'
PATCH_MAGIC = b'BTD2'
LEGACY_FRAME_MAGIC = b'BTF1'
LEGACY_COMPRESSED_MAGIC = b'BTZ1'
LEGACY_PATCH_MAGIC = b'BTD1'
FRAME_HEADER = struct.Struct('!4sIQQ7I4Q')
LEGACY_FRAME_HEADER = struct.Struct('!4sIQQ7I3Q')
COMPRESSED_SIZE = struct.Struct('!I')
PATCH_HEADER = struct.Struct('!QI')
PATCH_RANGE = struct.Struct('!II')
PATCH_LIMIT = 4 * 1024 * 1024
PATCH_BLOCK = 4096
PATCH_RANGES = 1024
COMPRESSED_LIMIT = 16 * 1024 * 1024
COMPRESS_MINIMUM = 32 * 1024
INPUT_HEADER = struct.Struct('!4sQII')
INPUT_REPLY = struct.Struct('!4sQI')
INPUT_NET_INTENT = struct.Struct('!8I3iI')
INPUT_MAGIC = b'BTI1'
INPUT_ACK = b'BTAI'
INPUT_LIMIT = 4 * 1024 * 1024 + 48
TEXT_LIMIT = 1024 * 1024
TRANSFER_CHUNK = 65536


class InputRejected(OSError):
    """The remote owner replied with an operation error on a live channel."""


def exact(sock, length):
    data = bytearray()
    while len(data) < length:
        block = sock.recv(min(TRANSFER_CHUNK, length - len(data)))
        if not block:
            raise EOFError('Remote semantic stream closed')
        data.extend(block)
    return bytes(data)


def authenticate_server(sock, token, view_token=None):
    supplied = exact(sock, 20)
    control = hmac.compare_digest(supplied[4:], token)
    observe = view_token is not None and hmac.compare_digest(supplied[4:], view_token)
    role = ('control' if control else 'observe' if observe else None) if supplied[:4] == AUTH_REQUEST else None
    sock.sendall(AUTH_ACCEPT if role else AUTH_REJECT)
    return role


def authenticate_client(sock, token):
    if len(token) != 16:
        raise ValueError('Remote token must be 16 bytes')
    sock.sendall(AUTH_REQUEST + token)
    if exact(sock, 4) != AUTH_ACCEPT:
        raise PermissionError('Remote semantic stream rejected the token')


def header(words, size, compressed=False, patch=False):
    if not 0 <= size <= FRAME_LIMIT:
        raise ValueError('Semantic frame exceeds the transport limit')
    magic = PATCH_MAGIC if patch else COMPRESSED_MAGIC if compressed else FRAME_MAGIC
    return FRAME_HEADER.pack(magic,
                             size, words[2], words[1],
                             words[12], words[13], words[14], words[15],
                             words[16], words[17], words[18],
                             words[3], words[4], words[5], time.monotonic_ns() // 1000000)


def compressed_frame(frame):
    if frame.size < COMPRESS_MINIMUM:
        return -1, 0
    limit = min(COMPRESSED_LIMIT, frame.size * 9 // 10)
    descriptor = os.memfd_create('batty-network-compressed', os.MFD_CLOEXEC)
    total = 0
    try:
        encoder = zlib.compressobj(level=1)
        offset = 0
        while offset < frame.size:
            block = os.pread(frame.fd, min(TRANSFER_CHUNK, frame.size - offset), offset)
            if not block:
                raise OSError('Sealed semantic frame ended before its advertised size')
            encoded = encoder.compress(block)
            if total + len(encoded) >= limit:
                os.close(descriptor)
                return -1, 0
            view = memoryview(encoded)
            while view:
                view = view[os.write(descriptor, view):]
            total += len(encoded)
            offset += len(block)
        encoded = encoder.flush()
        if total + len(encoded) >= limit:
            os.close(descriptor)
            return -1, 0
        view = memoryview(encoded)
        while view:
            view = view[os.write(descriptor, view):]
        total += len(encoded)
        return descriptor, total
    except BaseException:
        os.close(descriptor)
        raise


def send_patch(sock, frame, base, words):
    if (base is None or base.size != frame.size or base.epoch != frame.epoch or
            base.revision >= frame.revision or frame.size < COMPRESS_MINIMUM):
        return False
    ranges = []
    start = None
    changed = 0
    for offset in range(0, frame.size, PATCH_BLOCK):
        size = min(PATCH_BLOCK, frame.size - offset)
        before = os.pread(base.fd, size, offset)
        after = os.pread(frame.fd, size, offset)
        if len(before) != size or len(after) != size:
            raise OSError('Sealed semantic frame ended during patch comparison')
        if before != after:
            if start is None:
                start = offset
        elif start is not None:
            ranges.append((start, offset - start))
            changed += offset - start
            start = None
        if changed > PATCH_LIMIT or len(ranges) > PATCH_RANGES:
            return False
    if start is not None:
        ranges.append((start, frame.size - start))
        changed += frame.size - start
    encoded = PATCH_HEADER.size + len(ranges) * PATCH_RANGE.size + changed
    if (not ranges or len(ranges) > PATCH_RANGES or encoded > PATCH_LIMIT or
            encoded * 4 >= frame.size):
        return False
    sock.sendall(header(words, frame.size, patch=True))
    sock.sendall(PATCH_HEADER.pack(base.revision, len(ranges)))
    for offset, size in ranges:
        sock.sendall(PATCH_RANGE.pack(offset, size))
        remaining = size
        while remaining:
            chunk = os.pread(frame.fd, min(TRANSFER_CHUNK, remaining), offset)
            if not chunk:
                raise OSError('Sealed semantic frame ended during patch transfer')
            sock.sendall(chunk)
            offset += len(chunk)
            remaining -= len(chunk)
    return True


def send_frame(sock, frame, words, base=None):
    if frame and send_patch(sock, frame, base, words):
        return
    size = frame.size if frame else 0
    descriptor, encoded_size = compressed_frame(frame) if frame else (-1, 0)
    try:
        sock.sendall(header(words, size, descriptor >= 0))
        if descriptor >= 0:
            sock.sendall(COMPRESSED_SIZE.pack(encoded_size))
        if frame:
            source, remaining = (descriptor, encoded_size) if descriptor >= 0 else (frame.fd, size)
            offset = 0
            while offset < remaining:
                block = os.pread(source, min(TRANSFER_CHUNK, remaining - offset), offset)
                if not block:
                    raise OSError('Sealed semantic frame ended before its advertised size')
                sock.sendall(block)
                offset += len(block)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def receive_header(sock):
    magic = exact(sock, 4)
    if magic in (FRAME_MAGIC, COMPRESSED_MAGIC, PATCH_MAGIC):
        values = FRAME_HEADER.unpack(magic + exact(sock, FRAME_HEADER.size - 4))
    elif magic in (LEGACY_FRAME_MAGIC, LEGACY_COMPRESSED_MAGIC, LEGACY_PATCH_MAGIC):
        values = LEGACY_FRAME_HEADER.unpack(magic + exact(sock, LEGACY_FRAME_HEADER.size - 4)) + (0,)
    else:
        raise ValueError('Invalid remote semantic frame magic')
    magic, size, epoch, revision, cols, rows, cw, ch, child, exit_status, state, read, written, pending, timestamp = values
    if ((magic in (FRAME_MAGIC, COMPRESSED_MAGIC, PATCH_MAGIC) and not timestamp) or
            not 0 <= size <= FRAME_LIMIT or
            (magic in (COMPRESSED_MAGIC, PATCH_MAGIC, LEGACY_COMPRESSED_MAGIC, LEGACY_PATCH_MAGIC)
             and not size) or not epoch or not revision or
            not 1 <= cols <= 1000 or not 1 <= rows <= 1000 or
            not 1 <= cw <= 512 or not 1 <= ch <= 512 or child > 0x7fffffff or
            pending > 8 * 1024 * 1024):
        raise ValueError('Invalid remote semantic frame header')
    return values


def receive_input(sock):
    magic, number, kind, size = INPUT_HEADER.unpack(exact(sock, INPUT_HEADER.size))
    if magic != INPUT_MAGIC or not number or kind not in (3, 4, 5, 6, 10) or size > INPUT_LIMIT:
        raise ValueError('Invalid remote semantic input header')
    if (kind == 3 and size > 4 * 1024 * 1024 or kind == 4 and size < 48 or
            kind == 5 and size != 16 or kind == 6 and size != 1 or kind == 10 and size):
        raise ValueError('Invalid remote semantic input length')
    return number, kind, exact(sock, size)


def send_input(sock, number, kind, body):
    if not number or kind not in (3, 4, 5, 6, 10) or len(body) > INPUT_LIMIT:
        raise ValueError('Invalid remote semantic input')
    if kind == 6 and (len(body) != 1 or body[0] > 1):
        raise ValueError('Invalid remote text request')
    if kind == 10 and body:
        raise ValueError('Invalid remote clipboard request')
    sock.sendall(INPUT_HEADER.pack(INPUT_MAGIC, number, kind, len(body)) + body)
    magic, reply_number, error = INPUT_REPLY.unpack(exact(sock, INPUT_REPLY.size))
    if magic != INPUT_ACK or reply_number != number or error > 4095:
        raise ValueError('Invalid remote semantic input acknowledgment')
    payload = b''
    if kind in (6, 10):
        size = struct.unpack('!I', exact(sock, 4))[0]
        if size > TEXT_LIMIT + (kind == 10) or (error and size):
            raise ValueError('Invalid remote text response length')
        payload = exact(sock, size)
    if error:
        raise InputRejected(error, os.strerror(error))
    return payload
