#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Discover and serve two persistent source panes through one Kilix command."""
import json
import os
from pathlib import Path
import queue
import select
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from control import request
from kilix_semantic_wire import (COMPRESSED_MAGIC, COMPRESSED_SIZE,
                                 authenticate_client, exact, receive_header)
from kilix_stream import manifest_routes, parse_layout_update

root = Path(os.environ['BATTY_TEST_SESSION_DIR'])
control_root = root / 'control'
control_root.mkdir(mode=0o700)
env = os.environ | {'BATTY_CONFIG': '/dev/null', 'BATTY_KILIX_CONFIG': '/dev/null',
                    'BATTY_CONTROL_DIR': str(control_root)}
owners = []
frontend = server = view_server = viewer = follow_server = follow_viewer = follow_reader = ssh_viewer = None
forward_listener = forward_worker = None
forward_stop = threading.Event()


def until(check, label, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.03)
    raise AssertionError(label)


def frame_text(frame):
    cells, count = struct.unpack_from('<II', frame, 52)
    title = struct.unpack_from('<I', frame, 68)[0]
    start = 860 + title + cells * 31
    return ''.join(chr(value) for value in struct.unpack_from('<' + 'I' * count, frame, start))


def forward_one(listener, destination):
    listener.settimeout(0.2)
    try:
        while not forward_stop.is_set():
            try:
                incoming, _ = listener.accept()
                break
            except socket.timeout:
                continue
        else:
            return
        with incoming, socket.create_connection(('127.0.0.1', destination), timeout=3) as outgoing:
            while not forward_stop.is_set():
                for source in select.select([incoming, outgoing], [], [], 0.2)[0]:
                    block = source.recv(65536)
                    if not block:
                        return
                    (outgoing if source is incoming else incoming).sendall(block)
    except OSError:
        if not forward_stop.is_set():
            raise


try:
    for name, marker in (('serve-one', 'SOURCE_ONE'), ('serve-two', 'SOURCE_TWO')):
        code = f'import time; print("{marker}",flush=True); time.sleep(30)'
        owner = subprocess.Popen([str(ROOT / 'batty'), '--headless', '--session', name,
                                  '--session-dir', str(root), '--', sys.executable, '-u', '-c', code],
                                 cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        owners.append((name, owner))
        until(lambda: (root / f'{name}.sock').exists(), f'{name} owner startup')
        owner.terminate()
        owner.communicate(timeout=5)
        assert (root / f'{name}.sock').exists(), 'Named PTY owner stopped with its bootstrap frontend'
    frontend_log = root / 'source-frontend.log'
    with frontend_log.open('wb') as output:
        frontend = subprocess.Popen([str(ROOT / 'kilix'), '--session-dir', str(root),
                                     '--attach', 'serve-one', '--attach', 'serve-two'],
                                    cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
        endpoint = until(lambda: next(control_root.glob('front-*/control.sock'), None),
                         'source frontend endpoint')

        def two_panes():
            try:
                panes = request(str(endpoint), 'list')['panes']
                return panes if len(panes) == 2 else None
            except (OSError, RuntimeError):
                return None

        panes = until(two_panes, 'source workspace panes')
        assert all(p['persistent'] and not p['observe'] for p in panes)
        duplicate = subprocess.run([str(ROOT / 'kilix'), 'remote', 'serve-group',
                                    str(panes[0]['id']), str(panes[0]['id']),
                                    '--socket', str(endpoint)],
                                   cwd=ROOT, env=env, capture_output=True, text=True, timeout=8)
        assert duplicate.returncode != 0 and 'repeats a remote owner' in duplicate.stderr
        request(str(endpoint), 'resize', panes[0]['id'], struct.pack('<Bi', 1, 1500))
        request(str(endpoint), 'rename', panes[0]['id'], b'Source page')
        source_panes = request(str(endpoint), 'list')['panes']
        assert source_panes[0]['width'] > source_panes[1]['width'] + 100
        view_server = subprocess.Popen([str(ROOT / 'kilix'), 'remote', 'serve-group',
                                        '--all', '--view-only', '--socket', str(endpoint)],
                                       cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, bufsize=1)
        assert select.select([view_server.stdout], [], [], 10)[0], 'View-only group did not publish'
        view_manifest = json.loads(view_server.stdout.readline())
        assert view_manifest['schema'] == 'batty.remote-group/v3'
        assert all(route['control_token'] is None for route in view_manifest['routes'])
        view_path = root / 'view-manifest.json'
        descriptor = os.open(view_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(view_manifest, stream)
        view_options = SimpleNamespace(route=None, manifest=str(view_path), port_map=[], control=[])
        assert all(route[3] == 'observe' for route in manifest_routes(view_options))
        view_options.control = [str(panes[0]['id'])]
        try:
            manifest_routes(view_options)
        except ValueError as error:
            assert 'no control token' in str(error)
        else:
            raise AssertionError('View-only manifest granted control')
        view_server.terminate()
        view_server.communicate(timeout=7)
        view_server = None
        server = subprocess.Popen([str(ROOT / 'kilix'), 'remote', 'serve-group',
                                   '--all', '--socket', str(endpoint)],
                                  cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, bufsize=1)
        assert select.select([server.stdout], [], [], 10)[0], 'Group server did not publish its routes'
        manifest = json.loads(server.stdout.readline())
        routes = manifest['routes']
        assert manifest['schema'] == 'batty.remote-group/v3' and len(routes) == 2
        assert manifest['layout_hex'] == request(str(endpoint), 'checkpoint')['layout_hex']
        assert [route['pane_id'] for route in routes] == [p['id'] for p in panes]
        assert len({r['frame_port'] for r in routes}) == 2
        assert len({r['view_token'] for r in routes}) == 2
        assert len({r['control_token'] for r in routes}) == 2
        assert all(r['view_token'] != r['control_token'] for r in routes)
        assert all(r['route'].endswith(',observe') and
                   r['owner_epoch'] == p['session_epoch'] for r, p in zip(routes, panes))
        manifest_path = root / 'group-manifest.json'
        descriptor = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(manifest, stream)
        options = SimpleNamespace(route=None, manifest=str(manifest_path), port_map=[], control=[])
        parsed = manifest_routes(options)
        assert len(parsed) == 2 and all(route[3] == 'observe' for route in parsed)
        subset = {key: value for key, value in manifest.items()
                  if key not in ('layout_hex', 'appearance')}
        subset['schema'] = 'batty.remote-group/v2'
        subset_path = root / 'route-only.json'
        descriptor = os.open(subset_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(subset, stream)
        options.manifest = str(subset_path)
        assert len(manifest_routes(options)) == 2
        options.manifest = str(manifest_path)
        bad_layout = dict(manifest, layout_hex=manifest['layout_hex'][:16])
        bad_path = root / 'bad-layout.json'
        descriptor = os.open(bad_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(bad_layout, stream)
        options.manifest = str(bad_path)
        try:
            manifest_routes(options)
        except ValueError as error:
            assert 'layout header' in str(error)
        else:
            raise AssertionError('Truncated source layout was accepted')
        options.manifest = str(manifest_path)
        source_ports = {route['frame_port'] for route in routes} | {route['input_port'] for route in routes}
        alternate = next(port for port in range(40000, 65536) if port not in source_ports)
        options.port_map = [f'{routes[0]["frame_port"]}={alternate}']
        assert manifest_routes(options)[0][0] == alternate, 'Forwarded frame port was not applied'
        options.port_map = ['65535=40000'] if 65535 not in source_ports else ['65534=40000']
        try:
            manifest_routes(options)
        except ValueError as error:
            assert 'Unknown pane ID or source port' in str(error)
        else:
            raise AssertionError('Unknown source port was accepted')
        options.port_map = []
        manifest_path.chmod(0o644)
        try:
            manifest_routes(options)
        except ValueError as error:
            assert 'owned private regular file' in str(error)
        else:
            raise AssertionError('Permissive token manifest was accepted')
        manifest_path.chmod(0o600)

        follow_server = subprocess.Popen([str(ROOT / 'kilix'), 'remote', 'serve-group',
                                          '--all', '--view-only', '--follow',
                                          '--socket', str(endpoint)], cwd=ROOT, env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, bufsize=1)
        follow_lines = queue.Queue()
        def read_follow_lines():
            for line in follow_server.stdout:
                follow_lines.put(line)
        follow_reader = threading.Thread(target=read_follow_lines, daemon=True)
        follow_reader.start()
        first_line = follow_lines.get(timeout=10)
        live_manifest = json.loads(first_line)
        assert live_manifest['schema'] == 'batty.remote-group/v3'
        follow_log = root / 'follow-viewer.log'
        with follow_log.open('wb') as output:
            follow_viewer = subprocess.Popen([str(ROOT / 'kilix'), 'remote', 'view-group',
                                              '--stream', '--name', 'follow-view',
                                              '--session-dir', str(root / 'follow-mirrors')],
                                             cwd=ROOT, env=env | {'TMPDIR': str(root)},
                                             stdin=subprocess.PIPE, stdout=output,
                                             stderr=subprocess.STDOUT, text=True)
            follow_viewer.stdin.write(first_line)
            follow_viewer.stdin.flush()
            follow_endpoint = until(lambda: next(root.glob('btgc-*/front-*/control.sock'), None),
                                    'live viewer frontend')

            def follow_panes(title, widths):
                try:
                    listed = request(str(follow_endpoint), 'list')['panes']
                    return (listed if len(listed) == 2 and
                            all(pane['page_title'] == title for pane in listed) and
                            [pane['width'] for pane in listed] == widths else None)
                except (OSError, RuntimeError):
                    return None

            until(lambda: follow_panes('Source page', [p['width'] for p in source_panes]),
                  'live viewer initial layout')
            request(str(endpoint), 'rename', panes[0]['id'], b'Live page')
            first_update = follow_lines.get(timeout=10)
            sequence, _, status = parse_layout_update(first_update,
                                                       [p['pane_id'] for p in live_manifest['routes']], 0)
            assert sequence == 1 and status is None
            follow_viewer.stdin.write(first_update)
            follow_viewer.stdin.flush()
            until(lambda: follow_panes('Live page', [p['width'] for p in source_panes]),
                  'live viewer renamed page')
            request(str(endpoint), 'resize', panes[0]['id'], struct.pack('<Bi', 1, 600))
            live_widths = [pane['width'] for pane in request(str(endpoint), 'list')['panes']]
            assert live_widths != [pane['width'] for pane in source_panes]
            second_update = follow_lines.get(timeout=10)
            sequence, _, status = parse_layout_update(second_update,
                                                       [p['pane_id'] for p in live_manifest['routes']], 1)
            assert sequence == 2 and status is None
            follow_viewer.stdin.write(second_update)
            follow_viewer.stdin.flush()
            until(lambda: follow_panes('Live page', live_widths), 'live viewer resized split')
            extra_name = 'serve-three'
            extra_code = 'import time; print("SOURCE_THREE",flush=True); time.sleep(30)'
            extra_owner = subprocess.Popen([str(ROOT / 'batty'), '--headless', '--session', extra_name,
                                            '--session-dir', str(root), '--', sys.executable, '-u',
                                            '-c', extra_code], cwd=ROOT, env=env,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            owners.append((extra_name, extra_owner))
            until(lambda: (root / f'{extra_name}.sock').exists(), 'third owner startup')
            extra_owner.terminate()
            extra_owner.communicate(timeout=5)
            created = request(str(endpoint), 'session', panes[0]['id'],
                              bytes((1, 1)) + extra_name.encode() + b'\0')['id']
            added_update = follow_lines.get(timeout=10)
            added = json.loads(added_update)
            assert added['schema'] == 'batty.remote-layout/v2' and added['sequence'] == 3
            assert {route['pane_id'] for route in added['routes']} == {p['id'] for p in panes} | {created}
            follow_viewer.stdin.write(added_update)
            follow_viewer.stdin.flush()
            added_panes = until(lambda: (listed if len(listed := request(
                str(follow_endpoint), 'list')['panes']) == 3 else None),
                'live viewer added source pane')
            added_pane = next(pane for pane in added_panes if pane['session'] == 'follow-view-3')
            until(lambda: 'SOURCE_THREE' in request(str(follow_endpoint), 'dump', added_pane['id']),
                  'live viewer third owner text')
            assert follow_viewer.poll() is None
            request(str(endpoint), 'close', created)
            removed_update = follow_lines.get(timeout=10)
            removed = json.loads(removed_update)
            assert removed['schema'] == 'batty.remote-layout/v2' and removed['sequence'] == 4
            assert {route['pane_id'] for route in removed['routes']} == {p['id'] for p in panes}
            follow_viewer.stdin.write(removed_update)
            follow_viewer.stdin.flush()
            remaining = until(lambda: (listed if len(listed := request(
                str(follow_endpoint), 'list')['panes']) == 2 else None),
                'live viewer removed source pane')
            assert all(pane['session'] != 'follow-view-3' for pane in remaining)
            assert follow_viewer.poll() is None
        follow_viewer.terminate()
        follow_viewer.wait(timeout=7)
        follow_viewer = None
        follow_server.terminate()
        follow_server.wait(timeout=7)
        follow_reader.join(timeout=1)
        follow_server.stdout.close()
        follow_server.stderr.close()
        follow_server = None
        follow_reader = None
        request(str(endpoint), 'resize', panes[0]['id'], struct.pack('<Bi', 1, -600))
        request(str(endpoint), 'rename', panes[0]['id'], b'Source page')

        fake_ssh = root / 'fake-ssh'
        fake_ssh.write_text('''#!/usr/bin/env python3
import os, select, shlex, socket, sys
args = sys.argv[1:]
if '-W' not in args:
    command = shlex.split(args[-1])
    os.execv(command[0], command)
port = int(args[args.index('-W') + 1].rsplit(':', 1)[1])
with socket.create_connection(('127.0.0.1', port), timeout=5) as remote:
    while True:
        for source in select.select([0, remote], [], [], 10)[0]:
            block = os.read(0, 65536) if source == 0 else remote.recv(65536)
            if not block:
                sys.exit(0)
            if source == 0:
                remote.sendall(block)
            else:
                view = memoryview(block)
                while view:
                    view = view[os.write(1, view):]
''')
        fake_ssh.chmod(0o700)
        ssh_bin = os.environ.get('BATTY_TEST_SSH_BIN', str(fake_ssh))
        ssh_host = os.environ.get('BATTY_TEST_SSH_HOST', 'fakehost')
        ssh_log = root / 'ssh-group-viewer.log'
        with ssh_log.open('wb') as output:
            ssh_viewer = subprocess.Popen([str(ROOT / 'kilix'), 'remote', 'ssh-group', ssh_host,
                                           '--ssh-bin', ssh_bin,
                                           '--remote-kilix', str(ROOT / 'kilix'),
                                           '--source-socket', str(endpoint),
                                           '--control', str(panes[0]['id']),
                                           '--name', 'ssh-view',
                                           '--session-dir', str(root / 'ssh-mirrors')],
                                          cwd=ROOT, env=env | {'TMPDIR': str(root)},
                                          stdout=output, stderr=subprocess.STDOUT)
            ssh_endpoint = until(lambda: next(root.glob('btgc-*/front-*/control.sock'), None),
                                 'SSH group viewer frontend')
            ssh_initial = until(lambda: (listed if len(listed := request(
                str(ssh_endpoint), 'list')['panes']) == 2 else None),
                'SSH group initial panes')
            assert [pane['observe'] for pane in ssh_initial] == [False, True]
            request(str(ssh_endpoint), 'send', ssh_initial[0]['id'], b'TUNNEL_INPUT\n')
            until(lambda: 'TUNNEL_INPUT' in request(str(endpoint), 'dump', panes[0]['id']),
                  'SSH group controller input reached source owner')
            ssh_created = request(str(endpoint), 'session', panes[0]['id'],
                                  bytes((1, 1)) + extra_name.encode() + b'\0')['id']
            ssh_panes = until(lambda: (listed if len(listed := request(
                str(ssh_endpoint), 'list')['panes']) == 3 else None),
                'SSH group forwarded added pane')
            ssh_added = next(pane for pane in ssh_panes if pane['session'] == 'ssh-view-3')
            until(lambda: 'SOURCE_THREE' in request(str(ssh_endpoint), 'dump', ssh_added['id']),
                  'SSH group forwarded new pane text')
            request(str(endpoint), 'close', ssh_created)
            until(lambda: len(request(str(ssh_endpoint), 'list')['panes']) == 2,
                  'SSH group removed pane')
        ssh_viewer.terminate()
        ssh_viewer.wait(timeout=10)
        ssh_viewer = None

        frontend.terminate()
        frontend.wait(timeout=7)
        assert server.poll() is None and all((root / f'{name}.sock').exists() for name, _ in owners), (
            'Detaching source frontend stopped a remote owner or its group server')
        for route, marker in zip(routes, ('SOURCE_ONE', 'SOURCE_TWO')):
            with socket.create_connection(('127.0.0.1', route['frame_port']), timeout=3) as remote:
                remote.settimeout(5)
                authenticate_client(remote, bytes.fromhex(route['view_token']))
                deadline = time.monotonic() + 5
                while True:
                    assert time.monotonic() < deadline, f'{marker} semantic frame missing'
                    header = receive_header(remote)
                    if not header[1]:
                        continue
                    if header[0] == COMPRESSED_MAGIC:
                        encoded = COMPRESSED_SIZE.unpack(exact(remote, COMPRESSED_SIZE.size))[0]
                        frame = zlib.decompress(exact(remote, encoded))
                    else:
                        frame = exact(remote, header[1])
                    assert len(frame) == header[1] and frame[:8] == b'BTPRES01'
                    if marker in frame_text(frame):
                        break
        for _ in range(8):
            forward_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            forward_listener.bind(('127.0.0.1', 0))
            if forward_listener.getsockname()[1] not in source_ports:
                break
            forward_listener.close()
        else:
            raise AssertionError('Could not reserve a distinct forwarded port')
        forward_listener.listen(1)
        mapped_port = forward_listener.getsockname()[1]
        forward_worker = threading.Thread(target=forward_one,
                                          args=(forward_listener, routes[0]['frame_port']), daemon=True)
        forward_worker.start()
        viewer_env = env | {'TMPDIR': str(root)}
        viewer_log = root / 'manifest-viewer.log'
        with viewer_log.open('wb') as output:
            viewer = subprocess.Popen([str(ROOT / 'kilix'), 'remote', 'view-group',
                                       '--manifest', str(manifest_path),
                                       '--port-map', f'{routes[0]["frame_port"]}={mapped_port}',
                                       '--control', str(routes[1]['pane_id']),
                                       '--session-dir', str(root / 'mirrors'),
                                       '--name', 'manifest-view'],
                                      cwd=ROOT, env=viewer_env, stdout=output,
                                      stderr=subprocess.STDOUT)
            view_endpoint = until(lambda: next(root.glob('btgc-*/front-*/control.sock'), None),
                                  'manifest viewer frontend')

            def mirrored_panes():
                try:
                    listed = request(str(view_endpoint), 'list')['panes']
                    return listed if len(listed) == 2 else None
                except (OSError, RuntimeError):
                    return None

            def mapped_layout():
                listed = mirrored_panes()
                return (listed if listed and
                        all(pane['page_title'] == 'Source page' for pane in listed) and
                        [pane['width'] for pane in listed] ==
                        [pane['width'] for pane in source_panes] else None)

            mirrored = until(mapped_layout, 'manifest viewer source layout')
            assert [pane['observe'] for pane in mirrored] == [True, False]

            def mirrored_text():
                try:
                    return ('SOURCE_ONE' in request(str(view_endpoint), 'dump', mirrored[0]['id']) and
                            'SOURCE_TWO' in request(str(view_endpoint), 'dump', mirrored[1]['id']))
                except (OSError, RuntimeError):
                    return False

            until(mirrored_text, 'manifest routes deliver both owner presentations')
finally:
    if viewer is not None:
        viewer.terminate()
        try:
            viewer.wait(timeout=7)
        except subprocess.TimeoutExpired:
            viewer.kill()
            viewer.wait(timeout=3)
    forward_stop.set()
    if forward_listener is not None:
        forward_listener.close()
    if forward_worker is not None:
        forward_worker.join(timeout=3)
    if server is not None:
        server.terminate()
        try:
            server.communicate(timeout=7)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate(timeout=3)
    if follow_viewer is not None:
        follow_viewer.terminate()
        try:
            follow_viewer.wait(timeout=7)
        except subprocess.TimeoutExpired:
            follow_viewer.kill()
            follow_viewer.wait(timeout=3)
    if follow_server is not None:
        follow_server.terminate()
        try:
            follow_server.wait(timeout=7)
        except subprocess.TimeoutExpired:
            follow_server.kill()
            follow_server.wait(timeout=3)
        errors = follow_server.stderr.read()
        (root / 'follow-server.log').write_text(errors)
    if follow_reader is not None:
        follow_reader.join(timeout=1)
    if ssh_viewer is not None:
        ssh_viewer.terminate()
        try:
            ssh_viewer.wait(timeout=10)
        except subprocess.TimeoutExpired:
            ssh_viewer.kill()
            ssh_viewer.wait(timeout=3)
    if view_server is not None:
        view_server.terminate()
        try:
            view_server.communicate(timeout=7)
        except subprocess.TimeoutExpired:
            view_server.kill()
            view_server.communicate(timeout=3)
    if frontend is not None and frontend.poll() is None:
        frontend.terminate()
        try:
            frontend.wait(timeout=7)
        except subprocess.TimeoutExpired:
            frontend.kill()
            frontend.wait(timeout=3)
    for name, owner in owners:
        if owner.poll() is None:
            owner.terminate()
            try:
                owner.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                owner.kill()
                owner.communicate(timeout=3)
        subprocess.run([str(ROOT / 'batty'), '--terminate', name, '--session-dir', str(root)],
                       cwd=ROOT, env=env, capture_output=True, timeout=8, check=True)

print('PASS grouped source discovery, private manifest, port mapping, native viewer and detached owners')
