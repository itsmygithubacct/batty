#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Recover output and fresh shells after owner loss and runtime-directory loss."""
import base64
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from control import request
from kilix_frame_source import FrameSource
from kilix_recovery import HEADER, read
from kilix_workspace import restore_records


def wait_for(check, process=None, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        if process is not None and process.poll() is not None:
            raise AssertionError(f'Frontend exited: {process.returncode}')
        time.sleep(0.03)
    raise AssertionError('Durable recovery deadline')


def owner_pid(root, name):
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
        connection.connect(str(root / (name + '.sock')))
        return struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]


with tempfile.TemporaryDirectory(prefix='bt-durable-recovery-') as directory:
    base = Path(directory)
    runtime = base / 'runtime'
    runtime.mkdir(mode=0o700)
    durable = base / 'state/batty/recovery'
    cwd = base / 'working directory'
    cwd.mkdir()
    ready, launches = base / 'ready', base / 'launches'
    replay_marker = base / 'output-must-not-execute'
    env = os.environ | {
        'BATTY_SESSION_DIR': str(runtime), 'XDG_STATE_HOME': str(base / 'state'),
        'BATTY_KILIX_RECOVERY_DIR': str(durable), 'BATTY_SHELL': '/bin/sh',
        'BATTY_KILIX_CONFIG': str(ROOT / 'tests/automatic_recovery_config.bash'),
        'BATTY_AUTO_RECOVERY_READY': str(ready), 'BATTY_OFFLINE': '1',
        'BATTY_TRANSCRIPT_ENABLED': '0', 'BATTY_TRANSCRIPT_MAINTENANCE': '0',
    }
    program = base / 'program.py'
    program.write_text(
        'import os, sys\n'
        f'with open({str(launches)!r}, "a") as marker: marker.write("launch\\n")\n'
        'print("ARCHIVED_SCROLLBACK_START", flush=True)\n'
        f'print("touch {replay_marker}", flush=True)\n'
        'for i in range(80): print("historical line", i)\n'
        'print("\\033[38;2;13;200;75m\\033[4:3mARCHIVED_STYLED_OUTPUT λ界\\033[0m", flush=True)\n'
        f'print("\\033[2;3H\\033_Ga=T,f=32,s=2,v=2,i=71,c=2,r=2,q=2;{base64.b64encode(bytes([255, 0, 0, 255] * 4)).decode()}\\033\\\\\\033[6;1H", end="", flush=True)\n'
        'for line in sys.stdin: print(line.strip(), flush=True)\n')
    processes = []
    with (base / 'frontends.log').open('w+') as log:
        def launch(args=()):
            ready.unlink(missing_ok=True)
            process = subprocess.Popen([str(ROOT / 'kilix'), *args], env=env, cwd=cwd,
                                       stdout=log, stderr=log)
            processes.append(process)
            wait_for(ready.exists, process)
            endpoint = ready.read_text().strip()
            wait_for(lambda: request(endpoint, 'ping'), process)
            return process, endpoint

        def stop(process):
            process.terminate()
            process.wait(timeout=5)

        def snapshot_contains(token):
            for path in durable.glob('.kilix-layout-*.json'):
                try:
                    checkpoint = restore_records(path, return_document=True)
                    if len(checkpoint['panes']) != 2:
                        continue
                    pane = next(p for p in checkpoint['panes'] if p['session'] == first['session'])
                    data = (path.parent / pane['recovery_output']).read_bytes()
                    if token.encode() in data and pane['session_epoch'] == first['session_epoch']:
                        return path
                except (OSError, ValueError, KeyError, StopIteration):
                    pass
            return None

        try:
            source, endpoint = launch(('--', sys.executable, '-u', str(program)))
            first = request(endpoint, 'checkpoint')['panes'][0]
            wait_for(lambda: 'ARCHIVED_STYLED_OUTPUT' in request(endpoint, 'dump', first['id']), source)
            result = subprocess.run([str(ROOT / 'kilix'), 'new-pane', 'right', '--socket', endpoint,
                                     '--target', str(first['id']), '--', '/bin/cat'],
                                    env=env, capture_output=True, text=True, timeout=8)
            assert result.returncode == 0, result.stderr
            request(endpoint, 'rename', first['id'], 'Recovered λ page'.encode())
            request(endpoint, 'pane-rename', first['id'], b'Saved program')
            request(endpoint, 'send', first['id'], b'LATEST_DURABLE_OUTPUT\n')
            before = request(endpoint, 'checkpoint')
            snapshot = wait_for(lambda: snapshot_contains('LATEST_DURABLE_OUTPUT'), source)
            saved_pane = restore_records(snapshot, return_document=True)['panes'][0]
            archive = snapshot.parent / saved_pane['recovery_output']
            metadata = read(archive, archive.stem)
            assert metadata['argv'] == [sys.executable, '-u', str(program)]
            assert metadata['cwd'] == str(cwd)
            data = archive.read_bytes()
            vt = HEADER.unpack_from(data)[3]
            image_count, placement_count = struct.unpack_from('<II', data, HEADER.size + vt + 60)
            assert image_count and placement_count, 'Graphics were omitted from durable output'
            # Preserve a separate explicit checkpoint for the restart-on-request check.
            manual = base / 'saved workspace.json'
            subprocess.run([str(ROOT / 'kilix'), 'save', '--socket', endpoint, str(manual)],
                           env=env, check=True, timeout=8, stdout=log, stderr=log)
            source.kill()
            source.wait(timeout=5)
            for pane in before['panes']:
                os.kill(owner_pid(runtime, pane['session']), signal.SIGKILL)
            time.sleep(0.2)
            # This is the reboot condition: the entire session runtime is gone.
            shutil.rmtree(runtime)
            recovered, endpoint = launch()
            after = request(endpoint, 'checkpoint')
            assert len(after['panes']) == 2
            fresh = next(p for p in after['panes'] if p['title'] == 'Saved program')
            assert fresh['pid'] != first['pid'] and fresh['session_epoch'] != first['session_epoch']
            assert fresh['page_title'] == 'Recovered λ page'
            assert launches.read_text().splitlines() == ['launch'], 'Recovery restarted the old program'
            output = request(endpoint, 'dump', fresh['id'])
            assert 'ARCHIVED_SCROLLBACK_START' in output, 'Scrollback was lost'
            assert 'ARCHIVED_STYLED_OUTPUT λ界' in output and 'LATEST_DURABLE_OUTPUT' in output
            with FrameSource(runtime, fresh['session'], int(fresh['session_epoch'], 16)) as observer:
                images, placements = struct.unpack_from('<II', os.pread(observer.frame.fd, 68, 0), 60)
                assert images and placements, 'Restored graphics were lost'
                header = os.pread(observer.frame.fd, 76, 0)
                cells, points, _, _, title_length = struct.unpack_from('<5I', header, 52)
                cell_data = os.pread(observer.frame.fd, cells * 31, 860 + title_length)
                assert any(cell_data[i + 12] == 3 and cell_data[i + 25:i + 28] == bytes([13, 200, 75])
                           for i in range(0, len(cell_data), 31)), 'Styled output was lost'
                image_start = 860 + title_length + cells * 31 + points * 4
                assert os.pread(observer.frame.fd, 4, image_start + 25) == bytes([255, 0, 0, 255])
            request(endpoint, 'send', fresh['id'], b"printf 'FRESH_SHELL_OK\\n'; pwd\n")
            try:
                wait_for(lambda: '\nFRESH_SHELL_OK\n' in request(endpoint, 'dump', fresh['id']), recovered)
                assert os.readlink(f'/proc/{fresh["pid"]}/cwd') == str(cwd)
            except AssertionError:
                print('Fresh shell:', Path(f'/proc/{fresh["pid"]}/cmdline').read_bytes(),
                      os.readlink(f'/proc/{fresh["pid"]}/cwd'), file=sys.stderr)
                print('Fresh output:', repr(request(endpoint, 'dump', fresh['id'])[-1000:]), file=sys.stderr)
                raise
            assert launches.read_text().splitlines() == ['launch']
            assert not replay_marker.exists(), 'Archived output was executed as shell input'
            fresh_save = base / 'fresh workspace.json'
            subprocess.run([str(ROOT / 'kilix'), 'save', '--socket', endpoint, str(fresh_save)],
                           env=env, check=True, timeout=8, stdout=log, stderr=log)
            fresh_checkpoint = restore_records(fresh_save, return_document=True)
            fresh_pane = next(p for p in fresh_checkpoint['panes'] if p['session'] == fresh['session'])
            fresh_archive = fresh_save.parent / fresh_pane['recovery_output']
            assert read(fresh_archive, fresh_archive.stem)['argv'] == metadata['argv'], 'Original restart command was lost'
            stop(recovered)
            # A killed owner can leave an unconnectable socket pathname behind.
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as stale:
                stale.bind(str(runtime / (first['session'] + '.sock')))
                os.chmod(runtime / (first['session'] + '.sock'), 0o600)
            # Explicit request uses the original argument vector rather than a shell string.
            restarted, endpoint = launch(('--restore', str(manual), '--restart-programs'))
            wait_for(lambda: len(launches.read_text().splitlines()) == 2, restarted)
            assert len(request(endpoint, 'checkpoint')['panes']) == 2
            stop(restarted)
            # Corrupt output must fail without executing a saved program.
            checkpoint = restore_records(manual, return_document=True)
            corrupt = manual.parent / checkpoint['panes'][0]['recovery_output']
            contents = corrupt.read_bytes()
            corrupt.write_bytes(contents[:-1] + bytes([contents[-1] ^ 1]))
            result = subprocess.run([str(ROOT / 'kilix'), '--restore', str(manual), '--restart-programs'],
                                    env=env, cwd=cwd, stdout=log, stderr=log, timeout=8)
            assert result.returncode != 0 and len(launches.read_text().splitlines()) == 2
            print('PASS durable recovery: runtime loss, layout, styled text, scrollback, graphics, cwd, fresh shells, explicit restart and corrupt archive rejection')
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read(), file=sys.stderr)
            raise
        finally:
            for process in processes:
                if process.poll() is None:
                    stop(process)
            for endpoint in runtime.glob('kilix-auto-*.sock'):
                subprocess.run([str(ROOT / 'batty'), '--terminate', endpoint.stem,
                                '--session-dir', str(runtime)], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
