#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Bounded PCM audio plane for a remote Batty pane."""
from collections import deque
import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
import zlib

from kilix_semantic_wire import authenticate_client, exact

HELLO = struct.Struct('!4sIBQ')
BLOCK = struct.Struct('!4sIBQII')
HELLO_MAGIC = b'BTA2'
LEGACY_HELLO_MAGIC = b'BTAH'
RAW_MAGIC = b'BTAR'
ZIP_MAGIC = b'BTAC'
BLOCK_MS = 20
QUEUE_BLOCKS = 4
MAX_BLOCK = 128 * 1024
PLAYOUT_DELAY = 0.08


class MediaClock:
    """Map one source monotonic timeline to bounded local playout times."""

    def __init__(self):
        self.lock = threading.Lock()
        self.offset = None
        self.generation = 0

    def _deadline(self, timestamp, received=None, generation=None):
        if not timestamp:
            return None
        now = time.monotonic() if received is None else received
        candidate = now - timestamp / 1000
        with self.lock:
            if generation is not None and generation != self.generation:
                return None
            if self.offset is None or candidate < self.offset:
                self.offset = candidate
            return timestamp / 1000 + self.offset + PLAYOUT_DELAY

    def frame_deadline(self, timestamp, received=None, generation=None):
        return self._deadline(timestamp, received, generation)

    def audio_deadline(self, timestamp, received=None, generation=None):
        return self._deadline(timestamp, received, generation)

    def reset(self, generation=None):
        with self.lock:
            self.offset = None
            self.generation = self.generation + 1 if generation is None else generation


def geometry(rate, channels):
    if (not isinstance(rate, int) or not 8000 <= rate <= 384000 or
            not isinstance(channels, int) or not 1 <= channels <= 8):
        raise ValueError('Audio rate must be 8000..384000 Hz and channels 1..8')
    frames = max(1, round(rate * BLOCK_MS / 1000))
    size = frames * channels * 2
    if size > MAX_BLOCK:
        raise ValueError('Audio block exceeds the transport limit')
    return size, frames * 1000 / rate


def encode_block(pcm, rate, channels, timestamp):
    if not pcm or len(pcm) > MAX_BLOCK or len(pcm) % (channels * 2):
        raise ValueError('Invalid PCM audio block')
    compressed = zlib.compress(pcm, 1)
    encoded = compressed if len(compressed) < len(pcm) * 9 // 10 else pcm
    magic = ZIP_MAGIC if encoded is compressed else RAW_MAGIC
    return BLOCK.pack(magic, rate, channels, timestamp, len(pcm), len(encoded)) + encoded


def receive_block(stream, rate, channels):
    magic, supplied_rate, supplied_channels, timestamp, raw_size, encoded_size = BLOCK.unpack(
        exact(stream, BLOCK.size))
    if (magic not in (RAW_MAGIC, ZIP_MAGIC) or supplied_rate != rate or
            supplied_channels != channels or not 0 < raw_size <= MAX_BLOCK or
            raw_size % (channels * 2) or not 0 < encoded_size <= MAX_BLOCK or
            (magic == RAW_MAGIC and encoded_size != raw_size) or
            (magic == ZIP_MAGIC and encoded_size >= raw_size)):
        raise ValueError('Invalid remote audio block header')
    encoded = exact(stream, encoded_size)
    if magic == RAW_MAGIC:
        return timestamp, encoded
    try:
        inflater = zlib.decompressobj()
        pcm = inflater.decompress(encoded, raw_size + 1)
        if len(pcm) > raw_size or inflater.unconsumed_tail:
            raise ValueError('Compressed remote audio exceeds its declared size')
        pcm += inflater.flush(raw_size + 1 - len(pcm))
    except zlib.error as error:
        raise ValueError('Invalid compressed remote audio block') from error
    if (len(pcm) != raw_size or not inflater.eof or inflater.unused_data or
            inflater.unconsumed_tail):
        raise ValueError('Invalid compressed remote audio body')
    return timestamp, pcm


class AudioPeer:
    def __init__(self, budget):
        self.condition = threading.Condition()
        self.queue = deque()
        self.budget = budget
        self.window_start = 0.0
        self.window_bytes = 0
        self.dropped = 0
        self.closed = False

    def offer(self, packet, now):
        with self.condition:
            if self.closed:
                return
            if now - self.window_start >= 1:
                self.window_start, self.window_bytes = now, 0
            if self.budget and self.window_bytes + len(packet) > self.budget:
                self.dropped += 1
                return
            self.window_bytes += len(packet)
            if len(self.queue) == QUEUE_BLOCKS:
                self.queue.popleft()
                self.dropped += 1
            self.queue.append(packet)
            self.condition.notify()

    def pop(self, timeout):
        with self.condition:
            self.condition.wait_for(lambda: self.queue or self.closed, timeout=timeout)
            return self.queue.popleft() if self.queue else None

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


class AudioSource:
    def __init__(self, command, rate=48000, channels=2, budget=0):
        if not command or not isinstance(command, str) or '\0' in command:
            raise ValueError('Audio source command must be nonempty')
        if not isinstance(budget, int) or budget < 0 or budget > 32 * 1024 * 1024:
            raise ValueError('Audio budget must be 0..33554432 bytes per second')
        self.block_size, self.block_duration = geometry(rate, channels)
        self.command, self.rate, self.channels, self.budget = command, rate, channels, budget
        self.lock = threading.Lock()
        self.peers = set()
        self.process = None
        self.worker = None
        self.stopped = threading.Event()
        self.finished = threading.Event()

    def start(self):
        if self.process is not None:
            raise RuntimeError('Audio source already started')
        self.process = subprocess.Popen(['/bin/sh', '-c', self.command], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        start_new_session=True)
        os.set_blocking(self.process.stdout.fileno(), False)
        self.worker = threading.Thread(target=self._read, daemon=True)
        self.worker.start()

    def subscribe(self):
        peer = AudioPeer(self.budget)
        with self.lock:
            self.peers.add(peer)
        return peer

    def unsubscribe(self, peer):
        with self.lock:
            self.peers.discard(peer)
        peer.close()

    def _read(self):
        pending = bytearray()
        next_due = time.monotonic()
        descriptor = self.process.stdout.fileno()
        try:
            while not self.stopped.is_set():
                if not select.select([descriptor], [], [], 0.1)[0]:
                    continue
                part = os.read(descriptor, self.block_size - len(pending))
                if not part:
                    break
                pending.extend(part)
                if len(pending) != self.block_size:
                    continue
                now = time.monotonic()
                if now > next_due + 0.2:
                    next_due = now
                if next_due > now and self.stopped.wait(next_due - now):
                    break
                timestamp = round(next_due * 1000)
                packet = encode_block(bytes(pending), self.rate, self.channels, timestamp)
                pending.clear()
                with self.lock:
                    peers = tuple(self.peers)
                for peer in peers:
                    peer.offer(packet, time.monotonic())
                next_due += self.block_duration / 1000
        finally:
            self.finished.set()
            try:
                self.process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
            with self.lock:
                peers = tuple(self.peers)
            for peer in peers:
                peer.close()

    def close(self):
        self.stopped.set()
        process = self.process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if self.worker is not None:
            self.worker.join(timeout=3)
        if process is not None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
            process.stdout.close()


class AudioSink:
    def __init__(self, rate, channels, command=None):
        self.rate, self.channels = rate, channels
        self.pending = b''
        self.offset = 0
        self.dropped = 0
        self.next_timestamp = None
        self.process = None
        self.disabled = command == 'none'
        if self.disabled:
            return
        if command:
            argv = ['/bin/sh', '-c', command]
        elif shutil.which('pacat'):
            argv = ['pacat', '--playback', '--raw', '--format=s16le', '--rate', str(rate),
                    '--channels', str(channels), '--latency-msec=20']
        elif shutil.which('aplay'):
            argv = ['aplay', '-q', '-t', 'raw', '-f', 'S16_LE', '-r', str(rate), '-c', str(channels)]
        else:
            self.disabled = True
            return
        environment = os.environ | {'BATTY_AUDIO_RATE': str(rate), 'BATTY_AUDIO_CHANNELS': str(channels)}
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, env=environment,
                                        start_new_session=True)
        os.set_blocking(self.process.stdin.fileno(), False)

    def _flush(self):
        while self.pending and not self.disabled:
            try:
                written = os.write(self.process.stdin.fileno(), self.pending[self.offset:])
            except BlockingIOError:
                return
            except OSError:
                self.disabled = True
                return
            if not written:
                return
            self.offset += written
            if self.offset == len(self.pending):
                self.pending, self.offset = b'', 0

    def offer(self, timestamp, pcm):
        if self.disabled:
            return
        self._flush()
        if self.pending:
            self.dropped += 1
            return
        duration = len(pcm) * 1000 // (self.rate * self.channels * 2)
        gap = 0 if self.next_timestamp is None else max(0, min(200, timestamp - self.next_timestamp))
        silence = bytes(round(gap * self.rate / 1000) * self.channels * 2)
        self.pending = silence + pcm
        self.next_timestamp = timestamp + duration
        self._flush()

    def close(self):
        if self.process is None:
            return
        self._flush()
        self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if self.process.poll() is None:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                self.process.wait(timeout=2)


class AudioReceiver:
    def __init__(self, host, port, token, epoch, output=None, clock=None,
                 clock_generation=0):
        self.host, self.port, self.token, self.epoch = host, port, token, epoch
        self.output = output
        self.clock = clock
        self.condition = threading.Condition()
        self.generation = clock_generation
        self.socket = None
        self.stopped = threading.Event()
        self.worker = None
        self.failed = None
        self.received = 0

    def start(self):
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def retarget(self, host, port, token):
        with self.condition:
            self.host, self.port, self.token = host, port, token
            self.generation += 1
            self.failed = None
            connection = self.socket
            self.condition.notify_all()
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _run(self):
        pause = 0.1
        while not self.stopped.is_set():
            with self.condition:
                host, port, token, generation = self.host, self.port, self.token, self.generation
            connection = None
            sink = None
            try:
                with socket.create_connection((host, port), timeout=3) as connection:
                    with self.condition:
                        self.socket = connection
                    connection.settimeout(3)
                    authenticate_client(connection, token)
                    magic, rate, channels, epoch = HELLO.unpack(exact(connection, HELLO.size))
                    geometry(rate, channels)
                    if magic not in (HELLO_MAGIC, LEGACY_HELLO_MAGIC) or epoch != self.epoch:
                        raise ValueError('Remote audio owner epoch changed')
                    with self.condition:
                        if generation != self.generation:
                            continue
                    sink = AudioSink(rate, channels, self.output)
                    connection.settimeout(None)
                    pause = 0.1
                    while not self.stopped.is_set():
                        timestamp, pcm = receive_block(connection, rate, channels)
                        with self.condition:
                            if generation != self.generation:
                                break
                        if magic == HELLO_MAGIC and self.clock is not None:
                            deadline = self.clock.audio_deadline(timestamp, generation=generation)
                            if deadline is not None:
                                while not self.stopped.is_set() and deadline > time.monotonic():
                                    if self.stopped.wait(min(deadline - time.monotonic(), 0.2)):
                                        break
                                if self.stopped.is_set():
                                    break
                            with self.condition:
                                if generation != self.generation:
                                    break
                        sink.offer(timestamp, pcm)
                        self.received += 1
            except (PermissionError, ValueError) as error:
                with self.condition:
                    if generation == self.generation and not self.stopped.is_set():
                        self.failed = error
                        self.condition.wait_for(lambda: self.stopped.is_set() or
                                                generation != self.generation)
            except (ConnectionError, EOFError, OSError):
                with self.condition:
                    if generation == self.generation and not self.stopped.is_set():
                        self.condition.wait_for(lambda: self.stopped.is_set() or
                                                generation != self.generation,
                                                timeout=pause)
                        pause = min(2.0, pause * 2)
            finally:
                if sink is not None:
                    sink.close()
                with self.condition:
                    if self.socket is connection:
                        self.socket = None

    def close(self):
        self.stopped.set()
        with self.condition:
            connection = self.socket
            self.condition.notify_all()
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.worker is not None:
            self.worker.join(timeout=5)
