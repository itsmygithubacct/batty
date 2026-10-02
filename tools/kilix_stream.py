#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Serve or view a Batty semantic pane over a loopback connection."""
import argparse
import json
import os
from pathlib import Path
import queue
import re
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time

from control import apply_layout, events, request
from control_paths import resolve_endpoint
from kilix_frame_source import NAME
from kilix_panes import pane_snapshot, resolve
from kilix_stream_mirror import StreamMirror
from kilix_stream_server import StreamServer

ROOT = Path(__file__).resolve().parent.parent


def mirror_root(directory):
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR') or
                   Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'batty')
    return Path(directory or runtime / 'batty-remote').expanduser().absolute()


def remote_token(value):
    if not isinstance(value, str):
        raise ValueError('Remote token must be 32 hexadecimal digits')
    try:
        token = bytes.fromhex(value)
    except ValueError as error:
        raise ValueError('Remote token must be 32 hexadecimal digits') from error
    if len(token) != 16:
        raise ValueError('Remote token must be 32 hexadecimal digits')
    return token


def serve(args):
    if args.audio_source is None and (args.audio_rate != 48000 or args.audio_channels != 2 or
                                      args.audio_budget != 0):
        raise ValueError('Audio settings require --audio-source')
    if args.target:
        if args.session or args.session_dir:
            raise ValueError('Use a pane target or a named session, not both')
        pane = resolve(pane_snapshot(resolve_endpoint(args.socket)), args.target)
        owner = pane['batty_session']
        if not owner or not owner['name'] or not owner['directory'] or not owner['epoch']:
            raise ValueError('Semantic streaming requires a named persistent Batty pane')
        root, name, epoch = owner['directory'], owner['name'], int(owner['epoch'], 16)
    else:
        if not args.session or not args.session_dir:
            raise ValueError('Supply a pane target or both --session and --session-dir')
        root, name, epoch = args.session_dir, args.session, 0
    server = StreamServer(root, name, args.port, expected_epoch=epoch,
                          audio_source=args.audio_source, audio_rate=args.audio_rate,
                          audio_channels=args.audio_channels, audio_budget=args.audio_budget)
    print(f'kilix remote: 127.0.0.1:{server.port} token {server.token.hex()}',
          file=sys.stderr, flush=True)
    print(f'kilix remote: input-port {server.input_port}', file=sys.stderr, flush=True)
    if server.audio_port is not None:
        print(f'kilix remote: audio-port {server.audio_port}', file=sys.stderr, flush=True)
    print(f'kilix remote: view-token {server.view_token.hex()}', file=sys.stderr, flush=True)
    signal.signal(signal.SIGTERM, lambda *_: server.stopped.set())
    try:
        server.serve_forever()
    finally:
        server.close()


def serve_group(args):
    endpoint = resolve_endpoint(args.socket)
    snapshot = pane_snapshot(endpoint)
    if args.all:
        if args.target:
            raise ValueError('Choose explicit pane targets or --all, not both')
        selected = [pane for pane in snapshot['panes']
                    if pane['batty_session'] and not pane['batty_session']['observer']]
        selected.sort(key=lambda pane: (pane['page']['index'], pane['pane_id']))
    else:
        selected = [resolve(snapshot, target) for target in args.target]
    if not 2 <= len(selected) <= 64:
        raise ValueError('A remote group needs 2..64 named persistent panes')
    def validate_selected(panes):
        owners = set()
        for pane in panes:
            owner = pane['batty_session']
            if not owner or not owner['name'] or not owner['directory'] or not owner['epoch']:
                raise ValueError(f'Pane {pane["pane_id"]} is not a named persistent Batty pane')
            if owner['observer']:
                raise ValueError(f'Pane {pane["pane_id"]} is an observer; serve its owning pane')
            identity = (owner['directory'], owner['name'], owner['epoch'])
            if identity in owners:
                raise ValueError(f'Pane {pane["pane_id"]} repeats a remote owner')
            owners.add(identity)

    validate_selected(selected)
    checkpoint = request(endpoint, 'checkpoint')
    captured = {pane['id']: pane for pane in checkpoint['panes']}
    source_layout = None
    if set(captured) == {pane['pane_id'] for pane in selected}:
        if all(captured[pane['pane_id']]['session_epoch'] == pane['batty_session']['epoch']
               for pane in selected):
            source_layout = {'layout_hex': checkpoint['layout_hex'],
                             'appearance': {key: checkpoint['appearance'][key]
                                            for key in ('width', 'height')}}
    if args.follow and source_layout is None:
        raise ValueError('Live layout updates require a group covering every source pane')
    servers, workers = [], []
    stopped = threading.Event()
    try:
        for pane in selected:
            owner = pane['batty_session']
            servers.append(StreamServer(owner['directory'], owner['name'],
                                        expected_epoch=int(owner['epoch'], 16)))
        for server in servers:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            workers.append(worker)
            worker.start()
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        def route_for(pane, server):
            return {'pane_id': pane['pane_id'], 'page_index': pane['page']['index'],
                    'title': pane['title'], 'owner_epoch': f'{server.expected_epoch:016x}',
                    'frame_port': server.port, 'input_port': server.input_port,
                    'view_token': server.view_token.hex(),
                    'control_token': None if args.view_only else server.token.hex(),
                    'route': f'{server.port},{server.view_token.hex()},{server.input_port},observe'}
        routes = [route_for(pane, server) for pane, server in zip(selected, servers)]
        manifest = {'schema': 'batty.remote-group/v3' if source_layout else 'batty.remote-group/v2',
                    'host': '127.0.0.1', 'routes': routes}
        if source_layout:
            manifest.update(source_layout)
        print(json.dumps(manifest, ensure_ascii=True), flush=True)
        if args.follow:
            cursor, epoch, sequence = 0, '0', 0
            current_layout = source_layout['layout_hex']
            live = {route['pane_id']: (server, worker, route)
                    for route, server, worker in zip(routes, servers, workers)}
            while not stopped.is_set():
                if any(not worker.is_alive() for _, worker, _ in live.values()):
                    raise RuntimeError('A remote pane server stopped unexpectedly')
                try:
                    changes = events(endpoint, cursor, epoch, timeout=3)
                    cursor, epoch = changes['cursor'], changes['epoch']
                    changed = changes['reset'] or any(
                        event['kind'] != 'changed' or event['changes'] & 3
                        for event in changes['events'])
                    if not changed:
                        continue
                    checkpoint = request(endpoint, 'checkpoint')
                except (OSError, RuntimeError, KeyError, ValueError):
                    sequence += 1
                    print(json.dumps({'schema': 'batty.remote-layout/v1',
                                      'sequence': sequence, 'status': 'source-unavailable'}), flush=True)
                    break
                current_panes = {pane['id']: pane for pane in checkpoint['panes']}
                membership_changed = (set(current_panes) != set(live) or
                    any(current_panes[pane_id]['session_epoch'] != route['owner_epoch']
                        for pane_id, (_, _, route) in live.items()))
                if membership_changed:
                    try:
                        fresh = pane_snapshot(endpoint)['panes']
                        selected = [pane for pane in fresh if pane['pane_id'] in current_panes]
                        selected.sort(key=lambda pane: (pane['page']['index'], pane['pane_id']))
                        if not 1 <= len(selected) <= 64 or len(selected) != len(current_panes):
                            raise ValueError('Source pane set cannot be served')
                        validate_selected(selected)
                        if any(current_panes[pane['pane_id']]['session_epoch'] !=
                               pane['batty_session']['epoch'] for pane in selected):
                            raise ValueError('Source owner changed during capture')
                        new_live = {}
                        for pane in selected:
                            pane_id = pane['pane_id']
                            old = live.get(pane_id)
                            if old and old[2]['owner_epoch'] == pane['batty_session']['epoch']:
                                new_live[pane_id] = old
                                continue
                            owner = pane['batty_session']
                            server = StreamServer(owner['directory'], owner['name'],
                                                  expected_epoch=int(owner['epoch'], 16))
                            worker = threading.Thread(target=server.serve_forever, daemon=True)
                            servers.append(server)
                            workers.append(worker)
                            worker.start()
                            new_live[pane_id] = (server, worker, route_for(pane, server))
                        routes = [new_live[pane['pane_id']][2] for pane in selected]
                        sequence += 1
                        print(json.dumps({'schema': 'batty.remote-layout/v2',
                                          'sequence': sequence, 'host': '127.0.0.1',
                                          'routes': routes,
                                          'layout_hex': checkpoint['layout_hex'],
                                          'appearance': source_layout['appearance']}), flush=True)
                        for pane_id, (server, _, _) in live.items():
                            if new_live.get(pane_id) is None or new_live[pane_id][0] is not server:
                                server.close()
                        live = new_live
                        current_layout = checkpoint['layout_hex']
                    except (OSError, RuntimeError, KeyError, ValueError):
                        sequence += 1
                        print(json.dumps({'schema': 'batty.remote-layout/v1',
                                          'sequence': sequence, 'status': 'membership-changed'}), flush=True)
                        break
                    continue
                if checkpoint['layout_hex'] != current_layout:
                    current_layout = checkpoint['layout_hex']
                    sequence += 1
                    print(json.dumps({'schema': 'batty.remote-layout/v1',
                                      'sequence': sequence, 'pane_ids': list(live),
                                      'layout_hex': current_layout}), flush=True)
        while not stopped.wait(0.2):
            current_workers = [worker for _, worker, _ in live.values()] if args.follow else workers
            if any(not worker.is_alive() for worker in current_workers):
                raise RuntimeError('A remote pane server stopped unexpectedly')
    finally:
        for server in servers:
            server.close()
        for worker in workers:
            worker.join(timeout=3)


def view(args):
    if not 1 <= args.port <= 65535:
        raise ValueError('View port must be 1..65535')
    token = remote_token(args.token)
    root = mirror_root(args.session_dir)
    name = args.name or f'remote-{os.getpid()}'
    mirror = StreamMirror(args.host, args.port, token, root, name, input_port=args.input_port,
                          audio_port=args.audio_port, audio_output=args.audio_output)
    worker = threading.Thread(target=mirror.serve_forever, daemon=True)
    worker.start()
    viewer = None
    try:
        mirror.ready()
        if args.input_port is not None:
            mirror.connect_input()
        if args.audio_port is not None:
            if args.audio_output is None and not shutil.which('pacat') and not shutil.which('aplay'):
                print('kilix remote: no pacat or aplay found; audio will not play',
                      file=sys.stderr, flush=True)
            mirror.start_audio()
        command = [str(ROOT / 'batty'), '--observe' if args.observe or args.input_port is None else '--attach',
                   name, '--session-dir', str(root)]
        if args.headless:
            command.append('--headless')
        viewer = subprocess.Popen(command)
        return viewer.wait()
    finally:
        if viewer is not None and viewer.poll() is None:
            viewer.terminate()
            try:
                viewer.wait(timeout=5)
            except subprocess.TimeoutExpired:
                viewer.kill()
                viewer.wait(timeout=3)
        mirror.close()
        worker.join(timeout=3)


def group_route(value):
    parts = value.split(',')
    if len(parts) not in (2, 3, 4, 5):
        raise ValueError('Route must be FRAME_PORT,TOKEN[,INPUT_PORT[,observe|attach[,AUDIO_PORT]]]')
    role = parts[3] if len(parts) >= 4 else ('attach' if len(parts) == 3 else 'observe')
    if role not in ('observe', 'attach') or role == 'attach' and len(parts) < 3:
        raise ValueError('Remote pane role must be observe or attach')
    try:
        port = int(parts[0])
        input_port = int(parts[2]) if len(parts) >= 3 else None
        audio_port = int(parts[4]) if len(parts) == 5 else None
    except ValueError as error:
        raise ValueError('Remote route ports must be decimal numbers') from error
    if any(value is not None and not 1 <= value <= 65535 for value in (port,input_port,audio_port)):
        raise ValueError('Remote route ports must be 1..65535')
    return port, remote_token(parts[1]), input_port, role, audio_port


def strict_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate remote group JSON key')
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError('Invalid JSON number')

    return json.loads(data, object_pairs_hook=unique, parse_constant=invalid_constant)


class StreamRecords:
    """Bounded, unbuffered line reader that can stop before process shutdown."""
    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.pending = bytearray()

    def next(self, stopped=None):
        while True:
            end = self.pending.find(b'\n')
            if end >= 0:
                if end + 1 > 131072:
                    raise ValueError('Remote group stream record exceeds 128 KiB')
                record = bytes(self.pending[:end + 1])
                del self.pending[:end + 1]
                return record
            if len(self.pending) > 131072:
                raise ValueError('Remote group stream record exceeds 128 KiB')
            if stopped is not None and stopped.is_set():
                return None
            readable, _, _ = select.select([self.descriptor], [], [],
                                           0.2 if stopped is not None else None)
            if not readable:
                continue
            block = os.read(self.descriptor, 4096)
            if not block:
                if self.pending:
                    raise ValueError('Remote group stream ended inside a record')
                return None
            self.pending.extend(block)


def layout_members(layout_hex, count):
    if (not isinstance(layout_hex, str) or
            not re.fullmatch(r'[0-9a-f]{16,65536}', layout_hex) or len(layout_hex) % 2):
        raise ValueError('Invalid remote group layout encoding')
    raw = bytes.fromhex(layout_hex)
    if raw[:4] not in (b'BWL1', b'BWL2') or len(raw) < 8 + raw[5] * 10 or raw[5] != count:
        raise ValueError('Invalid remote group layout header')
    return [struct.unpack_from('<Q', raw, 8 + index * 10)[0]
            for index in range(raw[5])]


def parse_layout_update(data, pane_ids, last_sequence):
    document = strict_json(data)
    if (not isinstance(document, dict) or
            document.get('schema') != 'batty.remote-layout/v1' or
            type(document.get('sequence')) is not int or
            document['sequence'] != last_sequence + 1):
        raise ValueError('Invalid remote layout update header or sequence')
    status = document.get('status')
    if status is not None:
        if status not in ('source-unavailable', 'membership-changed'):
            raise ValueError('Invalid remote layout status')
        return document['sequence'], None, status
    if document.get('pane_ids') != pane_ids:
        raise ValueError('Remote layout update changed pane membership')
    layout_hex = document.get('layout_hex')
    if set(layout_members(layout_hex, len(pane_ids))) != set(pane_ids):
        raise ValueError('Remote layout update changed pane IDs')
    return document['sequence'], layout_hex, None


def manifest_routes(args, with_layout=False, data=None, minimum=2, allow_unused_maps=False):
    if data is not None and (args.route or args.manifest):
        raise ValueError('A remote group stream cannot also use --manifest or --route')
    if data is None and args.route and args.manifest:
        raise ValueError('Choose --manifest or --route, not both')
    if data is None and args.route:
        if args.port_map or args.control:
            raise ValueError('--port-map and --control require --manifest')
        routes = [group_route(value) for value in args.route]
        return (routes, None, None, None) if with_layout else routes
    if data is None:
        if not args.manifest:
            raise ValueError('Supply --manifest, --stream or repeat --route')
        path = Path(args.manifest).expanduser()
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                    info.st_mode & 0o077 or not 0 < info.st_size <= 131072):
                raise ValueError('Remote group manifest must be an owned private regular file of at most 128 KiB')
            data = stream.read(131073)
    if len(data) > 131072:
        raise ValueError('Remote group manifest exceeds 128 KiB')
    document = strict_json(data)
    if (not isinstance(document, dict) or document.get('schema') not in
            ('batty.remote-group/v2', 'batty.remote-group/v3') or
            document.get('host') != '127.0.0.1' or not isinstance(document.get('routes'), list) or
            not minimum <= len(document['routes']) <= 64):
        raise ValueError('Invalid remote group manifest header')
    mapping = {}
    for item in args.port_map:
        source, separator, local = item.partition('=')
        if not separator or not source.isascii() or not source.isdigit() or not local.isascii() or not local.isdigit():
            raise ValueError('Port mappings must be SOURCE=LOCAL decimal ports')
        source, local = int(source), int(local)
        if not 1 <= source <= 65535 or not 1 <= local <= 65535 or source in mapping:
            raise ValueError('Port mapping is duplicate or out of range')
        mapping[source] = local
    controls = set()
    for value in args.control:
        if not value.isascii() or not value.isdigit() or int(value) in controls:
            raise ValueError('Control pane IDs must be unique decimal numbers')
        controls.add(int(value))
    routes, pane_ids, identities, source_ports, local_ports = [], [], set(), set(), set()
    for item in document['routes']:
        if not isinstance(item, dict):
            raise ValueError('Invalid remote group pane')
        pane = item.get('pane_id')
        if type(pane) is not int or not 0 < pane < 2**64 or pane in identities:
            raise ValueError('Invalid or duplicate remote group pane ID')
        identities.add(pane)
        pane_ids.append(pane)
        if (type(item.get('page_index')) is not int or not 1 <= item['page_index'] <= 16 or
                not isinstance(item.get('title'), str) or
                not isinstance(item.get('owner_epoch'), str) or
                len(item['owner_epoch']) != 16):
            raise ValueError('Invalid remote group pane metadata')
        try:
            epoch = int(item['owner_epoch'], 16)
        except ValueError as error:
            raise ValueError('Invalid remote group owner epoch') from error
        if not 0 < epoch < 2**64 or not isinstance(item.get('route'), str):
            raise ValueError('Invalid remote group route')
        frame, token, input_port, role, audio_port = group_route(item['route'])
        view_token = remote_token(item.get('view_token', ''))
        control_value = item.get('control_token')
        control_token = None if control_value is None else remote_token(control_value)
        if (role != 'observe' or input_port is None or audio_port is not None or
                'control_token' not in item or
                view_token == control_token or token != view_token or
                type(item.get('frame_port')) is not int or item['frame_port'] != frame or
                type(item.get('input_port')) is not int or item['input_port'] != input_port or
                item.get('view_token') != view_token.hex() or
                control_value != (control_token.hex() if control_token is not None else None)):
            raise ValueError('Remote group route disagrees with its manifest fields')
        for port in (frame, input_port):
            if port in source_ports or mapping.get(port, port) in local_ports:
                raise ValueError('Remote group ports must be distinct')
            source_ports.add(port)
            local_ports.add(mapping.get(port, port))
        if pane in controls and control_token is None:
            raise ValueError(f'Pane {pane} has no control token in this view-only manifest')
        routes.append((mapping.get(frame, frame), control_token if pane in controls else view_token,
                       mapping.get(input_port, input_port),
                       'attach' if pane in controls else 'observe', None))
    if (not allow_unused_maps and controls - identities or
            not allow_unused_maps and set(mapping) - source_ports):
        raise ValueError('Unknown pane ID or source port in remote group options')
    layout_hex = size = None
    if document['schema'] == 'batty.remote-group/v3':
        layout_hex = document.get('layout_hex')
        saved = layout_members(layout_hex, len(routes))
        if set(saved) != identities:
            raise ValueError('Remote group layout panes disagree with routes')
        appearance = document.get('appearance')
        if (not isinstance(appearance, dict) or
                type(appearance.get('width')) is not int or
                type(appearance.get('height')) is not int or
                not 120 <= appearance['width'] <= 16384 or
                not 80 <= appearance['height'] <= 16384):
            raise ValueError('Invalid remote group source size')
        size = (appearance['width'], appearance['height'])
    return (routes, layout_hex, pane_ids, size) if with_layout else routes


def view_group(args):
    source = StreamRecords(sys.stdin.fileno()) if args.stream else None
    first_record = source.next() if source is not None else None
    if args.stream and first_record is None:
        raise ValueError('Remote group stream ended before its manifest')
    routes, layout_hex, pane_ids, source_size = manifest_routes(args, with_layout=True,
                                                               data=first_record)
    if args.stream and layout_hex is None:
        raise ValueError('Live remote group streams require a complete version 3 layout')
    if not 2 <= len(routes) <= 64:
        raise ValueError('A remote workspace needs 2..64 pane routes')
    root = mirror_root(args.session_dir)
    prefix = args.name or f'remote-{os.getpid()}'
    if len(prefix) > 45 or not NAME.fullmatch(prefix) or prefix.startswith('.'):
        raise ValueError('Remote workspace name must be 1..45 safe characters')
    mirrors, workers = [], []
    viewer = registry = view_endpoint = input_thread = input_stop = None
    try:
        for index, (port, token, input_port, role, audio_port) in enumerate(routes, 1):
            name = f'{prefix}-{index}'
            mirror = StreamMirror(args.host, port, token, root, name, input_port=input_port,
                                  audio_port=audio_port,
                                  audio_output=args.audio_output if audio_port is not None else None)
            mirrors.append(mirror)
            worker = threading.Thread(target=mirror.serve_forever, daemon=True)
            workers.append(worker)
            worker.start()
        for mirror in mirrors:
            mirror.ready()
            if mirror.input_port is not None:
                mirror.connect_input()
            if mirror.audio_port is not None:
                mirror.start_audio()
        command = [str(ROOT / 'kilix'), '--session-dir', str(root)]
        if source_size is not None:
            command += ['--width', str(source_size[0]), '--height', str(source_size[1])]
        for mirror, route in zip(mirrors, routes):
            command += ['--' + route[3],
                        mirror.endpoint.stem]
        env = os.environ.copy()
        if layout_hex is not None:
            registry = tempfile.TemporaryDirectory(prefix='btgc-')
            env['BATTY_CONTROL_DIR'] = registry.name
        viewer = subprocess.Popen(command, env=env)
        if layout_hex is not None:
            deadline = time.monotonic() + 12
            while True:
                if viewer.poll() is not None:
                    raise RuntimeError('Remote group viewer exited before its layout was applied')
                endpoints = list(Path(registry.name).glob('front-*/control.sock'))
                if len(endpoints) == 1:
                    try:
                        panes = request(str(endpoints[0]), 'list', timeout=0.5)['panes']
                    except (OSError, RuntimeError, ValueError):
                        panes = []
                    if len(panes) == len(mirrors):
                        by_name = {pane['session']: pane['id'] for pane in panes}
                        names = [mirror.endpoint.stem for mirror in mirrors]
                        if len(by_name) != len(names) or set(by_name) != set(names):
                            raise RuntimeError('Remote group viewer opened unexpected panes')
                        apply_layout(str(endpoints[0]), layout_hex,
                                     list(zip(pane_ids, (by_name[name] for name in names))))
                        view_endpoint = str(endpoints[0])
                        break
                if time.monotonic() >= deadline:
                    raise RuntimeError('Remote group viewer did not expose its panes for layout')
                time.sleep(0.05)
        if args.stream:
            updates = queue.Queue(maxsize=8)
            input_stop = threading.Event()

            def enqueue(record):
                while not input_stop.is_set():
                    try:
                        updates.put(record, timeout=0.2)
                        return
                    except queue.Full:
                        pass

            def read_updates():
                try:
                    while not input_stop.is_set():
                        record = source.next(input_stop)
                        enqueue(record)
                        if record is None:
                            return
                except (OSError, ValueError) as error:
                    enqueue(error)

            input_thread = threading.Thread(target=read_updates, daemon=True)
            input_thread.start()
            sequence = 0
            current_layout = layout_hex
            mirror_by_id = dict(zip(pane_ids, mirrors))
            worker_by_id = dict(zip(pane_ids, workers))
            local_by_id = dict(zip(pane_ids, (by_name[mirror.endpoint.stem] for mirror in mirrors)))
            route_by_id = dict(zip(pane_ids, routes))

            def add_pane(pane_id, route):
                used = {mirror.endpoint.stem for mirror in mirror_by_id.values()}
                name = next(f'{prefix}-{number}' for number in range(1, 65)
                            if f'{prefix}-{number}' not in used)
                port, token, input_port, role, audio_port = route
                mirror = StreamMirror(args.host, port, token, root, name,
                                      input_port=input_port, audio_port=audio_port,
                                      audio_output=args.audio_output if audio_port else None)
                worker = threading.Thread(target=mirror.serve_forever, daemon=True)
                try:
                    worker.start()
                    mirror.ready()
                    if input_port is not None:
                        mirror.connect_input()
                    if audio_port is not None:
                        mirror.start_audio()
                    anchor = next(iter(local_by_id.values()))
                    payload = bytes((1, 1 if role == 'attach' else 2)) + name.encode() + b'\0'
                    local_id = request(view_endpoint, 'session', anchor, payload)['id']
                except BaseException:
                    mirror.close()
                    if worker.ident is not None:
                        worker.join(timeout=3)
                    raise
                mirror_by_id[pane_id] = mirror
                worker_by_id[pane_id] = worker
                local_by_id[pane_id] = local_id
                route_by_id[pane_id] = route
                mirrors.append(mirror)
                workers.append(worker)

            def remove_pane(pane_id):
                request(view_endpoint, 'close', local_by_id[pane_id])
                mirror_by_id.pop(pane_id).close()
                worker_by_id.pop(pane_id).join(timeout=3)
                local_by_id.pop(pane_id)
                route_by_id.pop(pane_id)

            while viewer.poll() is None:
                try:
                    update = updates.get(timeout=0.2)
                except queue.Empty:
                    continue
                if update is None:
                    break
                try:
                    if isinstance(update, Exception):
                        raise update
                    document = strict_json(update)
                    if isinstance(document, dict) and document.get('schema') == 'batty.remote-layout/v2':
                        if type(document.get('sequence')) is not int or document['sequence'] != sequence + 1:
                            raise ValueError('Invalid remote group refresh sequence')
                        full = dict(document, schema='batty.remote-group/v3')
                        new_routes, new_layout, new_ids, _ = manifest_routes(
                            args, with_layout=True, data=json.dumps(full).encode(),
                            minimum=1, allow_unused_maps=True)
                        new_by_id = dict(zip(new_ids, new_routes))
                        removed = [pane_id for pane_id in pane_ids if pane_id not in new_by_id]
                        added = [pane_id for pane_id in new_ids if pane_id not in local_by_id]
                        while removed and len(local_by_id) + len(added) > 64:
                            if len(local_by_id) == 1:
                                break
                            remove_pane(removed.pop(0))
                        for pane_id in added:
                            add_pane(pane_id, new_by_id[pane_id])
                        for pane_id in removed:
                            remove_pane(pane_id)
                        for pane_id in new_ids:
                            if pane_id not in route_by_id or pane_id in added:
                                continue
                            if route_by_id[pane_id] != new_by_id[pane_id]:
                                port, token, input_port, role, audio_port = new_by_id[pane_id]
                                if role != route_by_id[pane_id][3]:
                                    raise ValueError('Remote pane input role changed')
                                mirror_by_id[pane_id].retarget(args.host, port, token,
                                                               input_port, audio_port)
                                route_by_id[pane_id] = new_by_id[pane_id]
                        apply_layout(view_endpoint, new_layout,
                                     [(pane_id, local_by_id[pane_id]) for pane_id in new_ids])
                        sequence, pane_ids, current_layout = document['sequence'], new_ids, new_layout
                    else:
                        sequence, new_layout, status = parse_layout_update(update, pane_ids, sequence)
                        if status is not None:
                            raise ValueError(f'source reported {status}')
                        if new_layout != current_layout:
                            apply_layout(view_endpoint, new_layout,
                                         [(pane_id, local_by_id[pane_id]) for pane_id in pane_ids])
                            current_layout = new_layout
                except (OSError, RuntimeError, ValueError) as error:
                    print(f'kilix remote: live layout updates stopped: {error}',
                          file=sys.stderr, flush=True)
                    break
        return viewer.wait()
    finally:
        if input_stop is not None:
            input_stop.set()
        if input_thread is not None:
            input_thread.join(timeout=1)
        if viewer is not None and viewer.poll() is None:
            viewer.terminate()
            try:
                viewer.wait(timeout=5)
            except subprocess.TimeoutExpired:
                viewer.kill()
                viewer.wait(timeout=3)
        for mirror in mirrors:
            mirror.close()
        for worker in workers:
            worker.join(timeout=3)
        if registry is not None:
            registry.cleanup()


def retarget(args):
    if not NAME.fullmatch(args.name) or args.name.startswith('.'):
        raise ValueError('Invalid Batty mirror session name')
    if args.host not in ('127.0.0.1', '::1', 'localhost'):
        raise ValueError('Semantic transport requires a loopback address or SSH tunnel')
    if (not 1 <= args.port <= 65535 or
            args.input_port is not None and not 1 <= args.input_port <= 65535 or
            args.audio_port is not None and not 1 <= args.audio_port <= 65535):
        raise ValueError('Remote ports must be 1..65535')
    token = remote_token(args.token)
    root = mirror_root(args.session_dir)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o777 != 0o700:
        raise PermissionError('Batty mirror root is not private')
    endpoint = root / f'{args.name}.control.sock'
    info = endpoint.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o777 != 0o600:
        raise PermissionError('Batty mirror control endpoint is not private')
    request = json.dumps({'host': args.host, 'port': args.port, 'token': token.hex(),
                          'input_port': args.input_port, 'audio_port': args.audio_port}).encode('utf-8')
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
        connection.settimeout(7)
        connection.connect(str(endpoint))
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        if int.from_bytes(credentials[4:8], sys.byteorder, signed=True) != os.geteuid():
            raise PermissionError('Batty mirror control peer has a different UID')
        connection.sendall(request)
        response = json.loads(connection.recv(512))
    if response.get('pending') is True:
        print(f'kilix remote: retarget pending for {args.name}')
        return
    if response.get('ok') is not True:
        raise RuntimeError(response.get('error', 'Remote mirror rejected retarget'))
    print(f'kilix remote: retargeted {args.name}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, prog='kilix remote')
    actions = parser.add_subparsers(dest='action', required=True)
    source = actions.add_parser('serve', help='Serve one named persistent pane')
    source.add_argument('target', nargs='?', help='Pane ID or unique pane target')
    source.add_argument('--socket', help='Explicit Batty control endpoint')
    source.add_argument('--session', help='Named persistent session without a frontend')
    source.add_argument('--session-dir', help='Private owner directory')
    source.add_argument('--port', type=int, default=0, help='Loopback TCP port; 0 chooses one')
    source.add_argument('--audio-source', help='Command writing raw signed-16-bit little-endian PCM')
    source.add_argument('--audio-rate', type=int, default=48000, help='Source sample rate in Hz')
    source.add_argument('--audio-channels', type=int, default=2, help='Source channel count')
    source.add_argument('--audio-budget', type=int, default=0, help='Per-viewer audio bytes per second; 0 is unlimited')
    source_group = actions.add_parser('serve-group', help='Serve selected persistent panes from one workspace')
    source_group.add_argument('target', nargs='*', help='Pane IDs or unique pane targets')
    source_group.add_argument('--all', action='store_true', help='Serve every owning persistent pane')
    source_group.add_argument('--follow', action='store_true',
                              help='Stream later layout and pane-set changes as JSON lines')
    source_group.add_argument('--view-only', action='store_true',
                              help='Omit control credentials from the printed manifest')
    source_group.add_argument('--socket', help='Explicit Batty control endpoint')
    receiver = actions.add_parser('view', help='Open a Batty viewer; add --input-port to control it')
    receiver.add_argument('--host', default='127.0.0.1', help='Loopback address or SSH tunnel endpoint')
    receiver.add_argument('--port', required=True, type=int)
    receiver.add_argument('--input-port', type=int, help='Forwarded input port for interactive control')
    receiver.add_argument('--observe', action='store_true',
                          help='Keep the local view read-only even with an input port for text queries')
    receiver.add_argument('--audio-port', type=int, help='Forwarded audio port, when serve enabled audio')
    receiver.add_argument('--audio-output', help='PCM playback command; defaults to pacat or aplay')
    receiver.add_argument('--token', required=True, help='32-hex-digit token printed by serve')
    receiver.add_argument('--name', help='Local temporary mirror session name; defaults to a unique name')
    receiver.add_argument('--session-dir', help='Private directory for the local mirror')
    receiver.add_argument('--headless', action='store_true', help='Receive without opening a window')
    group = actions.add_parser('view-group', help='Combine several remote panes in one Kilix workspace')
    group.add_argument('--host', default='127.0.0.1', help='Loopback address or SSH tunnel endpoint')
    group.add_argument('--route', action='append',
                       help='Repeat FRAME_PORT,TOKEN[,INPUT_PORT[,observe|attach[,AUDIO_PORT]]]')
    group.add_argument('--manifest', help='Private JSON manifest from remote serve-group')
    group.add_argument('--stream', action='store_true',
                       help='Read a version 3 manifest and later layout updates as JSON lines from stdin')
    group.add_argument('--port-map', action='append', default=[], metavar='SOURCE=LOCAL',
                       help='Forwarded local port for a manifest source port; repeat as needed')
    group.add_argument('--control', action='append', default=[], metavar='PANE_ID',
                       help='Grant local input to a manifest pane; default is observe')
    group.add_argument('--name', help='Prefix for local mirror session names')
    group.add_argument('--session-dir', help='Private directory for the local mirrors')
    group.add_argument('--audio-output', help='PCM playback command for audio routes')
    ssh_group_parser = actions.add_parser('ssh-group',
                                          help='Follow a source workspace over SSH with automatic pane forwarding')
    ssh_group_parser.add_argument('host', help='SSH host alias or user@host')
    ssh_group_parser.add_argument('--remote-kilix', default='kilix',
                                  help='Kilix executable on the source host; defaults to kilix in PATH')
    ssh_group_parser.add_argument('--source-socket', help='Source Batty control endpoint')
    ssh_group_parser.add_argument('--ssh-bin', default='ssh', help='SSH executable')
    ssh_group_parser.add_argument('--view-only', action='store_true',
                                  help='Omit control credentials from the source manifest')
    ssh_group_parser.add_argument('--control', action='append', default=[], metavar='PANE_ID',
                                  help='Allow local input to an initial source pane')
    ssh_group_parser.add_argument('--name', help='Prefix for local mirror session names')
    ssh_group_parser.add_argument('--session-dir', help='Private directory for local mirrors')
    move = actions.add_parser('retarget', help='Reconnect a running viewer to a restarted server')
    move.add_argument('--name', required=True, help='Viewer name supplied to remote view')
    move.add_argument('--session-dir', help='Private directory used by remote view')
    move.add_argument('--host', default='127.0.0.1', help='Loopback address or SSH tunnel endpoint')
    move.add_argument('--port', required=True, type=int, help='New forwarded frame port')
    move.add_argument('--input-port', type=int, help='New forwarded input port for an interactive view')
    move.add_argument('--audio-port', type=int, help='New forwarded audio port for an audio view')
    move.add_argument('--token', required=True, help='New token printed by remote serve')
    args = parser.parse_args()
    if args.action == 'serve':
        if not 0 <= args.port <= 65535:
            parser.error('--port must be 0..65535')
        serve(args)
        return 0
    if args.action == 'serve-group':
        serve_group(args)
        return 0
    if args.action == 'retarget':
        retarget(args)
        return 0
    if args.action == 'view-group':
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        return view_group(args)
    if args.action == 'ssh-group':
        from kilix_ssh_group import ssh_group
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        return ssh_group(args)
    if (args.input_port is not None and not 1 <= args.input_port <= 65535 or
            args.audio_port is not None and not 1 <= args.audio_port <= 65535):
        parser.error('Remote ports must be 1..65535')
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    return view(args)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, RuntimeError, ValueError) as error:
        print(f'kilix remote: {error}', file=sys.stderr)
        sys.exit(1)
