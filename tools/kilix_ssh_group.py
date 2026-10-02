#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Carry a changing Batty remote group over SSH without fixed port maps."""
import json
from pathlib import Path
import re
import shlex
import socket
import subprocess
import threading
from types import SimpleNamespace

from kilix_stream import StreamRecords, manifest_routes, strict_json

ROOT = Path(__file__).resolve().parent.parent
HOST = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.@:\[\]-]*\Z')


class SSHBridge:
    """One loopback listener; each accepted peer gets an SSH stdio channel."""

    def __init__(self, host, source_port, ssh_bin):
        self.host, self.source_port, self.ssh_bin = host, source_port, ssh_bin
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(8)
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.children = set()
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _reap(self, child):
        child.wait()
        with self.lock:
            self.children.discard(child)

    def _accept(self):
        while not self.stopped.is_set():
            try:
                peer, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stopped.is_set():
                    return
                raise
            with peer:
                try:
                    child = subprocess.Popen(
                        [self.ssh_bin, '-T', '-W', f'127.0.0.1:{self.source_port}',
                         '--', self.host], stdin=peer, stdout=peer)
                except OSError:
                    continue
                with self.lock:
                    if self.stopped.is_set():
                        child.terminate()
                    else:
                        self.children.add(child)
                        threading.Thread(target=self._reap, args=(child,), daemon=True).start()

    def close(self):
        self.stopped.set()
        self.listener.close()
        self.thread.join(timeout=1)
        with self.lock:
            children = list(self.children)
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)


class GroupRouter:
    def __init__(self, host, ssh_bin, controls):
        self.host, self.ssh_bin = host, ssh_bin
        self.controls = controls
        self.bridges = {}

    def rewrite(self, record, first=False):
        if len(record) > 65536:
            raise ValueError('Remote group stream record exceeds 64 KiB')
        document = strict_json(record)
        if not isinstance(document, dict):
            raise ValueError('Invalid remote group stream record')
        schema = document.get('schema')
        if first and schema != 'batty.remote-group/v3':
            raise ValueError('SSH group source did not publish a complete layout')
        if schema not in ('batty.remote-group/v3', 'batty.remote-layout/v2'):
            if first or schema != 'batty.remote-layout/v1':
                raise ValueError('Invalid remote group stream schema')
            return record, None
        source = dict(document)
        if schema == 'batty.remote-layout/v2':
            source['schema'] = 'batty.remote-group/v3'
        options = SimpleNamespace(route=[], manifest=None, port_map=[], control=self.controls)
        manifest_routes(options, with_layout=True, data=json.dumps(source).encode(),
                        minimum=1 if not first else 2, allow_unused_maps=not first)
        active = set()
        copied = dict(document)
        copied['routes'] = []
        for route in document['routes']:
            route = dict(route)
            for key in ('frame_port', 'input_port'):
                remote_port = route[key]
                active.add(remote_port)
                if remote_port not in self.bridges:
                    self.bridges[remote_port] = SSHBridge(self.host, remote_port, self.ssh_bin)
                route[key] = self.bridges[remote_port].port
            route['route'] = (f'{route["frame_port"]},{route["view_token"]},'
                              f'{route["input_port"]},observe')
            copied['routes'].append(route)
        result = (json.dumps(copied, ensure_ascii=True, separators=(',', ':')) + '\n').encode()
        if len(result) > 65536:
            raise ValueError('Forwarded remote group record exceeds 64 KiB')
        return result, active

    def prune(self, active):
        for port in set(self.bridges) - active:
            self.bridges.pop(port).close()

    def close(self):
        self.prune(set())


def ssh_group(args):
    if not HOST.fullmatch(args.host):
        raise ValueError('SSH host must be a simple host alias or user@host')
    if args.view_only and args.control:
        raise ValueError('--view-only cannot be combined with --control')
    remote = [args.remote_kilix, 'remote', 'serve-group', '--all', '--follow']
    if args.view_only:
        remote.append('--view-only')
    if args.source_socket:
        remote += ['--socket', args.source_socket]
    command = [args.ssh_bin, '-T', '--', args.host, shlex.join(remote)]
    source = viewer = None
    router = GroupRouter(args.host, args.ssh_bin, args.control)
    stopped = threading.Event()
    try:
        source = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        records = StreamRecords(source.stdout.fileno())
        first = records.next()
        if first is None:
            raise RuntimeError(f'SSH group source exited before its manifest ({source.wait()})')
        forwarded, _ = router.rewrite(first, first=True)
        viewer_command = [str(ROOT / 'kilix'), 'remote', 'view-group', '--stream']
        if args.name:
            viewer_command += ['--name', args.name]
        if args.session_dir:
            viewer_command += ['--session-dir', args.session_dir]
        for pane_id in args.control:
            viewer_command += ['--control', pane_id]
        viewer = subprocess.Popen(viewer_command, stdin=subprocess.PIPE)
        threading.Thread(target=lambda: (viewer.wait(), stopped.set()), daemon=True).start()
        viewer.stdin.write(forwarded)
        viewer.stdin.flush()
        while not stopped.is_set():
            record = records.next(stopped)
            if record is None:
                break
            forwarded, active = router.rewrite(record)
            try:
                viewer.stdin.write(forwarded)
                viewer.stdin.flush()
            except BrokenPipeError:
                break
            if active is not None:
                router.prune(active)
        viewer.stdin.close()
        return viewer.wait()
    finally:
        stopped.set()
        if viewer is not None and viewer.poll() is None:
            viewer.terminate()
            try:
                viewer.wait(timeout=5)
            except subprocess.TimeoutExpired:
                viewer.kill()
                viewer.wait(timeout=3)
        if source is not None:
            if source.poll() is None:
                source.terminate()
            try:
                source.wait(timeout=5)
            except subprocess.TimeoutExpired:
                source.kill()
                source.wait(timeout=3)
            source.stdout.close()
        router.close()
