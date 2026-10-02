#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Receive semantic frames and expose a local Batty viewer service."""
from array import array
import errno
import fcntl
import json
import os
from pathlib import Path
import socket
import stat
import struct
import threading
import time
import zlib

from kilix_frame_source import FRAME, HEADER, HELLO, MAGIC, NAME, SEALS, VERSION
from kilix_input_source import CLIPBOARD, INTENT, INTENT_LAYOUT, RESIZE, SEND, TEXT
from kilix_semantic_wire import (COMPRESSED_LIMIT, COMPRESSED_MAGIC, COMPRESSED_SIZE,
                                 LEGACY_COMPRESSED_MAGIC, LEGACY_PATCH_MAGIC,
                                 PATCH_HEADER, PATCH_LIMIT, PATCH_MAGIC, PATCH_RANGE, PATCH_RANGES,
                                 INPUT_NET_INTENT, InputRejected, authenticate_client, exact,
                                 receive_header, send_input)
from kilix_stream_audio import AudioReceiver, MediaClock

OBSERVERS = 8
TRANSFER_CHUNK = 65536


class StreamMirror:
    def __init__(self, host, port, token, root, name, input_port=None,
                 audio_port=None, audio_output=None):
        if host not in ('127.0.0.1', '::1', 'localhost'):
            raise ValueError('Semantic transport requires a loopback address or SSH tunnel')
        if not NAME.fullmatch(name) or name.startswith('.'):
            raise ValueError('Invalid Batty mirror session name')
        if len(token) != 16:
            raise ValueError('Remote token must be 16 bytes')
        if audio_port is not None and not 1 <= audio_port <= 65535:
            raise ValueError('Remote audio port must be 1..65535')
        if audio_output is not None and audio_port is None:
            raise ValueError('Audio output requires a remote audio port')
        directory = Path(root)
        if not directory.is_absolute():
            raise ValueError('Batty mirror root must be absolute')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o777 != 0o700:
            raise PermissionError('Batty mirror root is not private')
        self.endpoint = directory / f'{name}.sock'
        if self.endpoint.exists() or self.endpoint.is_symlink():
            raise FileExistsError(self.endpoint)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.bind(str(self.endpoint))
        os.chmod(self.endpoint, 0o600)
        self.endpoint_inode = self.endpoint.lstat().st_ino
        self.listener.listen(OBSERVERS)
        self.listener.settimeout(0.25)
        self.control_endpoint = directory / f'{name}.control.sock'
        if self.control_endpoint.exists() or self.control_endpoint.is_symlink():
            self.listener.close()
            self.endpoint.unlink()
            raise FileExistsError(self.control_endpoint)
        self.control_listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            self.control_listener.bind(str(self.control_endpoint))
        except BaseException:
            self.control_listener.close()
            self.listener.close()
            self.endpoint.unlink()
            raise
        os.chmod(self.control_endpoint, 0o600)
        self.control_inode = self.control_endpoint.lstat().st_ino
        self.control_listener.listen(1)
        self.control_listener.settimeout(0.25)
        self.host, self.port, self.token = host, port, token
        self.input_port = input_port
        self.audio_port = audio_port
        self.audio_output = audio_output
        self.audio_receiver = None
        self.media_clock = MediaClock()
        self.route_generation = 0
        self.input_socket = None
        self.input_lock = threading.Lock()
        self.input_number = 0
        self.clipboard_policy = False
        self.condition = threading.Condition(threading.RLock())
        self.frame_fd = -1
        self.frame_size = 0
        self.metadata = None
        self.failed = None
        self.connected = False
        self.reconnects = 0
        self.stopped = threading.Event()
        self.clients = set()
        self.controller = None
        self.workers = set()
        self.receiver = None
        self.control_thread = None
        self.network_socket = None

    def retarget(self, host, port, token, input_port, audio_port=None):
        if host not in ('127.0.0.1', '::1', 'localhost'):
            raise ValueError('Semantic transport requires a loopback address or SSH tunnel')
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError('Remote frame port must be 1..65535')
        if not isinstance(token, bytes) or len(token) != 16:
            raise ValueError('Remote token must be 16 bytes')
        if input_port is not None and (not isinstance(input_port, int) or not 1 <= input_port <= 65535):
            raise ValueError('Remote input port must be 1..65535')
        if audio_port is not None and (not isinstance(audio_port, int) or not 1 <= audio_port <= 65535):
            raise ValueError('Remote audio port must be 1..65535')
        with self.condition:
            if self.stopped.is_set():
                raise RuntimeError('Remote mirror is closing')
            if (self.input_port is None) != (input_port is None):
                raise ValueError('Retarget must preserve the viewer input role')
            if (self.audio_port is None) != (audio_port is None):
                raise ValueError('Retarget must preserve the viewer audio role')
            self.host, self.port, self.token, self.input_port = host, port, token, input_port
            self.audio_port = audio_port
            self.route_generation += 1
            generation = self.route_generation
            self.failed = None
            self.connected = False
            network = self.network_socket
            self.condition.notify_all()
        self.media_clock.reset(generation)
        with self.input_lock:
            old_input = self.input_socket
            self.input_socket = None
            self.input_number = 0
            if old_input is not None:
                old_input.close()
        if network is not None:
            try:
                network.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.audio_receiver is not None:
            self.audio_receiver.retarget(host, audio_port, token)
        return generation

    def start_audio(self):
        with self.condition:
            if self.audio_port is None or self.metadata is None or self.audio_receiver is not None:
                raise ValueError('Remote audio is unavailable or already started')
            receiver = AudioReceiver(self.host, self.audio_port, self.token,
                                     self.metadata[2], self.audio_output, self.media_clock,
                                     clock_generation=self.route_generation)
            self.audio_receiver = receiver
        receiver.start()

    def _control(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.control_listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stopped.is_set():
                    break
                raise
            with connection:
                try:
                    peer = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    if struct.unpack('3i', peer)[1] != os.geteuid():
                        continue
                    connection.settimeout(7)
                    data = connection.recv(512)
                    if not data or len(data) >= 512:
                        raise ValueError('Invalid remote retarget request')
                    request = json.loads(data)
                    if not isinstance(request, dict) or set(request) != {'host', 'port', 'token', 'input_port', 'audio_port'}:
                        raise ValueError('Invalid remote retarget request')
                    token = bytes.fromhex(request['token'])
                    generation = self.retarget(request['host'], request['port'], token,
                                               request['input_port'], request['audio_port'])
                    with self.condition:
                        self.condition.wait_for(lambda: self.connected or self.failed is not None or
                                                self.stopped.is_set() or
                                                self.route_generation != generation,
                                                timeout=4)
                        if self.failed is not None:
                            response = {'error': f'Remote semantic stream failed: {self.failed}'}
                        elif self.connected:
                            response = {'ok': True}
                        else:
                            response = {'pending': True}
                except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
                    response = {'error': str(error)[:160]}
                try:
                    connection.sendall(json.dumps(response).encode('utf-8'))
                except OSError:
                    pass

    def _receive_frame(self, stream, values):
        size, epoch, revision, cols, rows, cw, ch = values[1:8]
        fd = os.memfd_create('batty-remote-frame', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        try:
            if values[0] in (COMPRESSED_MAGIC, LEGACY_COMPRESSED_MAGIC):
                encoded_size = COMPRESSED_SIZE.unpack(exact(stream, COMPRESSED_SIZE.size))[0]
                if not 0 < encoded_size < size or encoded_size > COMPRESSED_LIMIT:
                    raise ValueError('Invalid compressed semantic frame length')
                remaining, decoded = encoded_size, 0
                inflater = zlib.decompressobj()
                while remaining:
                    block = exact(stream, min(remaining, TRANSFER_CHUNK))
                    plain = inflater.decompress(block, size - decoded + 1)
                    decoded += len(plain)
                    if decoded > size or inflater.unconsumed_tail or inflater.unused_data:
                        raise ValueError('Compressed semantic frame exceeds its declared body')
                    view = memoryview(plain)
                    while view:
                        view = view[os.write(fd, view):]
                    remaining -= len(block)
                plain = inflater.flush(size - decoded + 1)
                decoded += len(plain)
                if decoded != size or not inflater.eof or inflater.unused_data:
                    raise ValueError('Compressed semantic frame is incomplete')
                view = memoryview(plain)
                while view:
                    view = view[os.write(fd, view):]
            else:
                remaining = size
                while remaining:
                    block = exact(stream, min(remaining, TRANSFER_CHUNK))
                    view = memoryview(block)
                    while view:
                        view = view[os.write(fd, view):]
                    remaining -= len(block)
            head = os.pread(fd, 48, 0)
            if (len(head) != 48 or head[:8] != b'BTPRES01' or
                    struct.unpack_from('<IIQQIIII', head, 8) !=
                    (1, size, epoch, revision, cols, rows, cw, ch)):
                raise ValueError('Remote semantic frame metadata disagrees with its body')
            fcntl.fcntl(fd, fcntl.F_ADD_SEALS, SEALS)
            return fd
        except zlib.error as error:
            os.close(fd)
            raise ValueError('Invalid compressed semantic frame') from error
        except BaseException:
            os.close(fd)
            raise

    def _receive_patch(self, stream, values, base_fd, base_revision):
        size, epoch, revision, cols, rows, cw, ch = values[1:8]
        old_revision, count = PATCH_HEADER.unpack(exact(stream, PATCH_HEADER.size))
        if (old_revision != base_revision or not 1 <= count <= PATCH_RANGES or
                size != self.frame_size):
            raise ValueError('Remote semantic patch base disagrees with its frame')
        fd = os.memfd_create('batty-remote-frame', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        try:
            offset = 0
            while offset < size:
                block = os.pread(base_fd, min(TRANSFER_CHUNK, size - offset), offset)
                if not block:
                    raise ValueError('Remote semantic patch base was truncated')
                view = memoryview(block)
                while view:
                    view = view[os.write(fd, view):]
                offset += len(block)
            used = PATCH_HEADER.size
            end = 0
            for _ in range(count):
                start, length = PATCH_RANGE.unpack(exact(stream, PATCH_RANGE.size))
                used += PATCH_RANGE.size + length
                if (not length or start < end or start + length > size or
                        used > PATCH_LIMIT):
                    raise ValueError('Invalid remote semantic patch range')
                body = exact(stream, length)
                view = memoryview(body)
                at = start
                while view:
                    written = os.pwrite(fd, view, at)
                    view = view[written:]
                    at += written
                end = start + length
            head = os.pread(fd, 48, 0)
            if (len(head) != 48 or head[:8] != b'BTPRES01' or
                    struct.unpack_from('<IIQQIIII', head, 8) !=
                    (1, size, epoch, revision, cols, rows, cw, ch)):
                raise ValueError('Remote semantic patch metadata disagrees with its body')
            fcntl.fcntl(fd, fcntl.F_ADD_SEALS, SEALS)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _network(self):
        pause = 0.1
        while not self.stopped.is_set():
            with self.condition:
                host, port, token, generation = self.host, self.port, self.token, self.route_generation
            stream = None
            try:
                with socket.create_connection((host, port), timeout=3) as stream:
                    with self.condition:
                        self.network_socket = stream
                        if generation != self.route_generation:
                            continue
                    stream.settimeout(5)
                    authenticate_client(stream, token)
                    with self.condition:
                        if generation != self.route_generation:
                            continue
                    stream.settimeout(None)
                    initial = True
                    while not self.stopped.is_set():
                        values = receive_header(stream)
                        with self.condition:
                            if generation != self.route_generation:
                                break
                        size, epoch, revision = values[1:4]
                        with self.condition:
                            previous = self.metadata
                        if previous and (epoch != previous[2] or revision < previous[3] or
                                         (size and revision == previous[3] and
                                          (not initial or size != self.frame_size or
                                           values[4:8] != previous[4:8])) or
                                         (not size and (revision != previous[3] or
                                                        values[4:8] != previous[4:8]))):
                            raise ValueError('Remote semantic owner or revision changed')
                        if values[10] & 64:
                            raise ValueError('Remote semantic stream supplied an unsupported owner delta')
                        if initial and not size:
                            raise ValueError('Remote semantic stream omitted its initial frame')
                        if values[0] in (PATCH_MAGIC, LEGACY_PATCH_MAGIC):
                            if initial or previous is None or self.frame_fd < 0:
                                raise ValueError('Remote semantic stream supplied a patch without a base')
                            with self.condition:
                                base_fd = os.dup(self.frame_fd)
                            try:
                                descriptor = self._receive_patch(stream, values, base_fd, previous[3])
                            finally:
                                os.close(base_fd)
                        else:
                            descriptor = self._receive_frame(stream, values) if size else -1
                        deadline = (self.media_clock.frame_deadline(values[14], generation=generation)
                                    if size else None)
                        if deadline is not None:
                            delay = deadline - time.monotonic()
                            if delay > 0:
                                self.stopped.wait(min(delay, 0.2))
                        with self.condition:
                            if generation != self.route_generation:
                                if descriptor >= 0:
                                    os.close(descriptor)
                                break
                            if descriptor >= 0:
                                old = self.frame_fd
                                self.frame_fd = descriptor
                                self.frame_size = size
                                if old >= 0:
                                    os.close(old)
                            self.metadata = values
                            if initial:
                                if self.connected is False and previous is not None:
                                    self.reconnects += 1
                                self.connected = True
                            self.condition.notify_all()
                        initial = False
                        pause = 0.1
            except (PermissionError, RuntimeError, ValueError) as error:
                if not self.stopped.is_set():
                    with self.condition:
                        if generation == self.route_generation:
                            self.failed = error
                            self.connected = False
                            self.condition.notify_all()
                            self.condition.wait_for(lambda: self.stopped.is_set() or
                                                    generation != self.route_generation)
            except (ConnectionError, EOFError, OSError):
                with self.condition:
                    if generation == self.route_generation:
                        self.connected = False
                        self.condition.notify_all()
                        self.condition.wait_for(lambda: self.stopped.is_set() or
                                                generation != self.route_generation,
                                                timeout=pause)
                        pause = min(2.0, pause * 2)
            finally:
                with self.condition:
                    if self.network_socket is stream:
                        self.network_socket = None

    def start(self):
        if self.receiver is not None:
            raise RuntimeError('Remote mirror already started')
        self.receiver = threading.Thread(target=self._network, daemon=True)
        self.receiver.start()

    def ready(self, timeout=5):
        with self.condition:
            if self.metadata is None and self.failed is None:
                self.condition.wait_for(lambda: self.metadata is not None or self.failed is not None,
                                        timeout=timeout)
            if self.failed is not None:
                raise RuntimeError(f'Remote semantic stream failed: {self.failed}')
            if self.metadata is None:
                raise TimeoutError('Remote semantic stream supplied no initial frame')

    def connect_input(self):
        with self.input_lock:
            if self.input_port is None or self.input_socket is not None:
                raise ValueError('Remote input port is unavailable or already connected')
            self._connect_input()

    def _connect_input(self):
        with self.condition:
            host, input_port, token, generation = (self.host, self.input_port,
                                                   self.token, self.route_generation)
        stream = socket.create_connection((host, input_port), timeout=3)
        try:
            stream.settimeout(5)
            authenticate_client(stream, token)
            if exact(stream, 4) != b'BTIR':
                raise RuntimeError('Remote input peer did not become ready')
            number = 0
            if self.clipboard_policy:
                policy = INPUT_NET_INTENT.pack(7, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1)
                send_input(stream, 1, INTENT, policy)
                number = 1
            with self.condition:
                if generation != self.route_generation:
                    raise ConnectionError('Remote input route changed during authentication')
            self.input_socket = stream
            self.input_number = number
        except BaseException:
            stream.close()
            raise

    @staticmethod
    def _request(connection):
        raw, ancillary, flags, _ = connection.recvmsg(HEADER.size + 16384,
                                                       socket.CMSG_SPACE(4 * array('i').itemsize),
                                                       socket.MSG_CMSG_CLOEXEC)
        descriptors = []
        try:
            for level, kind, payload in ancillary:
                if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS or len(payload) % 4:
                    raise ValueError('Malformed local observer descriptor')
                received = array('i')
                received.frombytes(payload)
                descriptors.extend(received)
            if not raw and not descriptors:
                return None
            if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(descriptors) > 1 or len(raw) < HEADER.size:
                raise ValueError('Malformed local observer request')
            words = HEADER.unpack(raw[:HEADER.size])
            if (words[7:9] != (MAGIC, VERSION) or words[22] or not words[0] or
                    words[21] > 48 or len(raw) != HEADER.size + words[21]):
                raise ValueError('Malformed local observer header')
            kind, size = words[9], words[6]
            if words[21] != (48 if kind == INTENT else 0):
                raise ValueError('Malformed local input intent')
            if (size > 4 * 1024 * 1024 or bool(size) != bool(descriptors) or
                    (size and kind not in (SEND, INTENT))):
                raise ValueError('Malformed local input payload')
            body = b''
            if descriptors:
                fd = descriptors[0]
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_size != size or
                        fcntl.fcntl(fd, fcntl.F_GET_SEALS) & SEALS != SEALS):
                    raise ValueError('Unsafe local input payload')
                parts = []
                offset = 0
                while offset < size:
                    part = os.pread(fd, min(size - offset, TRANSFER_CHUNK), offset)
                    if not part:
                        raise ValueError('Local input payload ended early')
                    parts.append(part)
                    offset += len(part)
                body = b''.join(parts)
            return words, raw[HEADER.size:], body
        finally:
            for fd in descriptors:
                os.close(fd)

    def _forward(self, kind, words, intent, body):
        policy_change = False
        desired_policy = False
        if kind == INTENT:
            fields = INTENT_LAYOUT.unpack(intent)
            if fields[0] == 5:
                # Focus belongs to this local view.
                return 0, b''
            if fields[0] == 7:
                policy_change = True
                desired_policy = bool(fields[11])
            body = INPUT_NET_INTENT.pack(*fields) + body
        elif kind == RESIZE:
            body = struct.pack('!4I', *words[12:16])
        elif kind == TEXT:
            if words[10] > 1:
                return errno.EINVAL, b''
            body = bytes([words[10]])
        with self.input_lock:
            if policy_change:
                self.clipboard_policy = desired_policy
            with self.condition:
                if not self.connected:
                    return (0 if policy_change else errno.ENOTCONN), b''
            if self.input_socket is None:
                try:
                    self._connect_input()
                except (ConnectionError, EOFError, OSError, PermissionError, RuntimeError, ValueError):
                    return (0 if policy_change else errno.ENOTCONN), b''
            try:
                self.input_number += 1
                result = send_input(self.input_socket, self.input_number, kind, body)
                return 0, result
            except InputRejected as error:
                return error.errno or errno.EIO, b''
            except (ConnectionError, EOFError, OSError, RuntimeError, ValueError):
                # The owner may have accepted this operation before its ACK
                # was lost. Never replay it on a new connection.
                self.input_socket.close()
                self.input_socket = None
                return (0 if policy_change else errno.EIO), b''

    def _reply(self, connection, request, error=0, descriptor=-1, blob_size=None):
        with self.condition:
            values = self.metadata
            if values is None:
                raise RuntimeError('Remote semantic mirror has no frame')
            _, epoch, revision, cols, rows, cw, ch, child, exit_status, state, read, written, pending = values[1:14]
            if self.input_port is None:
                state &= ~(8 | 16)  # Read-only viewers cannot claim clipboard policy.
            if not self.connected:
                state |= 1 << 10
            reply = [0] * 23
            reply[:7] = [request[0], revision, epoch, read, written, pending,
                         (self.frame_size if blob_size is None else blob_size) if descriptor >= 0 else 0]
            reply[7:11] = [MAGIC, VERSION, request[9], 0]
            reply[11:21] = [error, cols, rows, cw, ch, child, exit_status, state,
                            0, len(self.clients)]
            ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                          array('i', [descriptor]).tobytes())] if descriptor >= 0 else []
            connection.sendmsg([HEADER.pack(*reply)], ancillary)

    def _client(self, connection):
        role = 0
        number = 0
        try:
            while not self.stopped.is_set():
                received = self._request(connection)
                if received is None:
                    break
                request, intent, body = received
                if request[0] != number + 1:
                    break
                number = request[0]
                kind = request[9]
                if kind == HELLO:
                    if role or request[10] not in (1, 2):
                        self._reply(connection, request, errno.EPERM)
                        continue
                    with self.condition:
                        if request[10] == 1:
                            if self.input_port is None or self.controller is not None:
                                self._reply(connection, request, errno.EBUSY)
                                continue
                            self.controller = connection
                        role = request[10]
                        self._reply(connection, request, descriptor=self.frame_fd)
                elif kind == FRAME and role and request[10] <= 100:
                    with self.condition:
                        if request[2] == self.metadata[2] and request[1] == self.metadata[3]:
                            self.condition.wait_for(lambda: self.metadata[3] != request[1] or
                                                    self.stopped.is_set(),
                                                    timeout=request[10] / 1000)
                        descriptor = -1 if (request[2], request[1]) == (self.metadata[2], self.metadata[3]) else self.frame_fd
                        self._reply(connection, request, descriptor=descriptor)
                elif kind in (SEND, INTENT, RESIZE, TEXT, CLIPBOARD) and (role == 1 or kind == TEXT and role == 2):
                    error, data = self._forward(kind, request, intent, body)
                    descriptor = -1
                    try:
                        if not error and kind in (TEXT, CLIPBOARD) and data:
                            descriptor = os.memfd_create('batty-remote-clipboard' if kind == CLIPBOARD else 'batty-remote-text',
                                                         os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
                            view = memoryview(data)
                            while view:
                                view = view[os.write(descriptor, view):]
                            fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, SEALS)
                    except OSError as failure:
                        error = failure.errno or errno.EIO
                        if descriptor >= 0:
                            os.close(descriptor)
                            descriptor = -1
                    try:
                        self._reply(connection, request, error, descriptor,
                                    len(data) if kind in (TEXT, CLIPBOARD) else None)
                    finally:
                        if descriptor >= 0:
                            os.close(descriptor)
                else:
                    self._reply(connection, request, errno.EPERM)
        except (ConnectionError, OSError, RuntimeError, ValueError):
            pass
        finally:
            with self.condition:
                if self.controller is connection:
                    self.controller = None
                self.clients.discard(connection)
                self.workers.discard(threading.current_thread())
            connection.close()

    def serve_forever(self):
        self.start()
        self.control_thread = threading.Thread(target=self._control, daemon=True)
        self.control_thread.start()
        while not self.stopped.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stopped.is_set():
                    break
                raise
            credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if struct.unpack('3i', credentials)[1] != os.geteuid():
                connection.close()
                continue
            with self.condition:
                # A bad remote route must not discard a retained local view.
                # The reply carries its disconnected state until retargeting.
                if len(self.clients) >= OBSERVERS or self.metadata is None:
                    connection.close()
                    continue
                self.clients.add(connection)
                worker = threading.Thread(target=self._client, args=(connection,), daemon=True)
                self.workers.add(worker)
                worker.start()

    def close(self):
        self.stopped.set()
        self.listener.close()
        self.control_listener.close()
        if self.audio_receiver is not None:
            self.audio_receiver.close()
        if self.network_socket is not None:
            try:
                self.network_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        input_stream = self.input_socket
        if input_stream is not None:
            try:
                input_stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            with self.input_lock:
                input_stream.close()
                if self.input_socket is input_stream:
                    self.input_socket = None
        with self.condition:
            self.condition.notify_all()
            clients = list(self.clients)
            workers = list(self.workers)
        for connection in clients:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for worker in workers:
            worker.join(timeout=4)
        if self.receiver is not None:
            self.receiver.join(timeout=5)
        if self.control_thread is not None:
            self.control_thread.join(timeout=4)
        with self.condition:
            if self.frame_fd >= 0:
                os.close(self.frame_fd)
                self.frame_fd = -1
        try:
            if self.endpoint.lstat().st_ino == self.endpoint_inode:
                self.endpoint.unlink()
        except FileNotFoundError:
            pass
        try:
            if self.control_endpoint.lstat().st_ino == self.control_inode:
                self.control_endpoint.unlink()
        except FileNotFoundError:
            pass
