#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Loopback, token-authenticated semantic stream from a Batty PTY owner."""
import errno
import os
import select
import socket
import struct
import threading
import time

from kilix_frame_source import FrameSource
from kilix_input_source import CLIPBOARD, InputSource, INTENT_LAYOUT, TEXT
from kilix_semantic_wire import (INPUT_ACK, INPUT_NET_INTENT, INPUT_REPLY, authenticate_server,
                                 receive_input, send_frame)
from kilix_stream_audio import AudioSource, HELLO, HELLO_MAGIC

CLIENT_LIMIT = 4


class StreamServer:
    def __init__(self, root, name, port=0, token=None, expected_epoch=0,
                 audio_source=None, audio_rate=48000, audio_channels=2, audio_budget=0):
        with FrameSource(root, name, expected_epoch) as source:
            owner_epoch = source.epoch  # Pin reuse of this name across clients.
        self.root, self.name, self.expected_epoch = root, name, owner_epoch
        self.token = token if token is not None else os.urandom(16)
        if len(self.token) != 16:
            raise ValueError('Remote token must be 16 bytes')
        self.view_token = os.urandom(16)
        while self.view_token == self.token:
            self.view_token = os.urandom(16)
        self.audio_source = (AudioSource(audio_source, audio_rate, audio_channels, audio_budget)
                             if audio_source is not None else None)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', port))
        self.listener.listen(CLIENT_LIMIT)
        self.listener.settimeout(0.25)
        self.port = self.listener.getsockname()[1]
        self.input_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.input_listener.bind(('127.0.0.1', 0))
        self.input_listener.listen(1)
        self.input_listener.settimeout(0.25)
        self.input_port = self.input_listener.getsockname()[1]
        self.audio_listener = None
        self.audio_port = None
        if self.audio_source is not None:
            self.audio_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.audio_listener.bind(('127.0.0.1', 0))
            self.audio_listener.listen(CLIENT_LIMIT)
            self.audio_listener.settimeout(0.25)
            self.audio_port = self.audio_listener.getsockname()[1]
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.clients = set()
        self.workers = set()
        self.input_thread = None
        self.audio_thread = None
        self.audio_clients = set()

    def _client(self, connection):
        try:
            connection.settimeout(3)
            if not authenticate_server(connection, self.token, self.view_token):
                return
            with FrameSource(self.root, self.name, self.expected_epoch) as source:
                send_frame(connection, source.frame, source.latest_words)
                previous = source.frame
                last_status = tuple(source.latest_words[index] for index in (3, 4, 5, 16, 17, 18))
                try:
                    while not self.stopped.is_set():
                        frame = source.poll(100)
                        status = tuple(source.latest_words[index] for index in (3, 4, 5, 16, 17, 18))
                        if frame:
                            try:
                                send_frame(connection, frame, source.latest_words, previous)
                            except BaseException:
                                frame.close()
                                raise
                            if previous is not source.frame:
                                previous.close()
                            previous = frame
                            last_status = status
                        else:
                            if status != last_status:
                                send_frame(connection, None, source.latest_words)
                                last_status = status
                            readable, _, _ = select.select([connection], [], [], 0)
                            if readable:
                                # The frame socket is output-only; input uses its own port.
                                return
                finally:
                    if previous is not source.frame:
                        previous.close()
        except (ConnectionError, EOFError, OSError, RuntimeError, ValueError):
            pass
        finally:
            with self.lock:
                self.clients.discard(connection)
                self.workers.discard(threading.current_thread())
            connection.close()

    def _input_client(self, connection):
        try:
            connection.settimeout(3)
            role = authenticate_server(connection, self.token, self.view_token)
            if not role:
                return
            with self.lock:
                worker = threading.current_thread()
                if role == 'control' and any(peer is not worker and
                                             getattr(peer, 'input_role', None) == 'control'
                                             for peer in self.workers):
                    return
                worker.input_role = role
            with InputSource(self.root, self.name, self.expected_epoch,
                             read_only=role == 'observe') as source:
                connection.sendall(b'BTIR')
                connection.settimeout(None)
                number = 0
                while not self.stopped.is_set():
                    incoming, kind, body = receive_input(connection)
                    if incoming != number + 1:
                        raise ValueError('Remote input sequence changed')
                    number = incoming
                    payload = b''
                    try:
                        if role == 'observe' and kind != TEXT:
                            error = errno.EPERM
                        elif kind == 3:
                            source.send(body)
                        elif kind == 4:
                            fields = INPUT_NET_INTENT.unpack(body[:INTENT_LAYOUT.size])
                            source.intent(fields, body[INTENT_LAYOUT.size:])
                        elif kind == 5:
                            source.resize(*struct.unpack('!4I', body))
                        elif kind == TEXT:
                            if body[0] > 1:
                                raise ValueError('Invalid remote text selection')
                            payload = source.text(bool(body[0]))
                        else:
                            payload = source.clipboard()
                        if role == 'control' or kind == TEXT:
                            error = 0
                    except (OSError, RuntimeError, ValueError) as failure:
                        error = failure.errno if isinstance(failure, OSError) else errno.EINVAL
                        if not 0 < error <= 4095:
                            error = errno.EIO
                    connection.sendall(INPUT_REPLY.pack(INPUT_ACK, number, error))
                    if kind in (TEXT, CLIPBOARD):
                        connection.sendall(struct.pack('!I', len(payload)) + payload)
        except (ConnectionError, EOFError, OSError, RuntimeError, ValueError):
            pass
        finally:
            with self.lock:
                self.clients.discard(connection)
                self.workers.discard(threading.current_thread())
            connection.close()

    def _input_accept(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.input_listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stopped.is_set():
                    break
                raise
            with self.lock:
                if len(self.clients) >= CLIENT_LIMIT:
                    connection.close()
                    continue
                self.clients.add(connection)
                worker = threading.Thread(target=self._input_client, args=(connection,), daemon=True)
                self.workers.add(worker)
                worker.start()

    def _audio_client(self, connection):
        peer = None
        try:
            connection.settimeout(3)
            if not authenticate_server(connection, self.token, self.view_token):
                return
            with FrameSource(self.root, self.name, self.expected_epoch) as owner:
                source = self.audio_source
                connection.sendall(HELLO.pack(HELLO_MAGIC, source.rate, source.channels,
                                              self.expected_epoch))
                connection.settimeout(0.5)
                peer = source.subscribe()
                checked = time.monotonic()
                while not self.stopped.is_set():
                    packet = peer.pop(0.2)
                    if packet is not None:
                        connection.sendall(packet)
                    elif source.finished.is_set():
                        break
                    if time.monotonic() - checked >= 1:
                        frame = owner.poll(0)
                        if frame is not None:
                            frame.close()
                        checked = time.monotonic()
                    if not packet and select.select([connection], [], [], 0)[0]:
                        break
        except (ConnectionError, EOFError, OSError, RuntimeError, ValueError):
            pass
        finally:
            if peer is not None:
                self.audio_source.unsubscribe(peer)
            with self.lock:
                self.clients.discard(connection)
                self.audio_clients.discard(connection)
                self.workers.discard(threading.current_thread())
            connection.close()

    def _audio_accept(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.audio_listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stopped.is_set():
                    break
                raise
            with self.lock:
                if len(self.audio_clients) >= CLIENT_LIMIT:
                    connection.close()
                    continue
                self.clients.add(connection)
                self.audio_clients.add(connection)
                worker = threading.Thread(target=self._audio_client, args=(connection,), daemon=True)
                self.workers.add(worker)
                worker.start()

    def serve_forever(self):
        if self.audio_source is not None:
            self.audio_source.start()
            self.audio_thread = threading.Thread(target=self._audio_accept, daemon=True)
            self.audio_thread.start()
        self.input_thread = threading.Thread(target=self._input_accept, daemon=True)
        self.input_thread.start()
        while not self.stopped.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stopped.is_set():
                    break
                raise
            with self.lock:
                if len(self.clients) >= CLIENT_LIMIT:
                    connection.close()
                    continue
                self.clients.add(connection)
                worker = threading.Thread(target=self._client, args=(connection,), daemon=True)
                self.workers.add(worker)
                worker.start()

    def close(self):
        self.stopped.set()
        self.listener.close()
        self.input_listener.close()
        if self.audio_listener is not None:
            self.audio_listener.close()
        with self.lock:
            clients = list(self.clients)
            workers = list(self.workers)
        for connection in clients:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for worker in workers:
            worker.join(timeout=4)
        if self.input_thread is not None:
            self.input_thread.join(timeout=4)
        if self.audio_thread is not None:
            self.audio_thread.join(timeout=4)
        if self.audio_source is not None:
            self.audio_source.close()
