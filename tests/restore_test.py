#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Save and restore real Kilix frontends around surviving session owners."""
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from control import request
from kilix_workspace import restore_records


def wait_for(check, process=None):
    deadline = time.monotonic() + 7
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        if process is not None and process.poll() is not None:
            raise AssertionError(f'Frontend exited: {process.returncode}')
        time.sleep(0.02)
    raise AssertionError('Restore test deadline')


def remapped_layout(hex_data, identities):
    data = bytearray.fromhex(hex_data)
    def identity(at):
        value, = struct.unpack_from('<Q', data, at)
        if value:
            struct.pack_into('<Q', data, at, identities[value])
    at = 8
    for _ in range(data[5]):
        identity(at)
        at += 10
    if data[:4] == b'BWL2':
        for _ in range(data[5]):
            at += 1 + data[at]
    for _ in range(data[4]):
        at += 1
        for _ in range(3):
            identity(at)
            at += 8
        at += 1 + data[at]
        for _ in range(3):
            nodes = data[at + 2]
            at += 3
            for _ in range(nodes):
                identity(at + 2)
                at += 17
    assert at == len(data)
    return bytes(data)


with tempfile.TemporaryDirectory(prefix='bt-restore-') as directory:
    private = Path(directory)
    ready = private / 'ready'
    saved = private / 'workspace.json'
    env = os.environ | {'BATTY_KILIX_CONFIG': str(ROOT / 'tests/restore_config.bash'),
                        'BATTY_SESSION_DIR': os.environ['BATTY_TEST_SESSION_DIR'],
                        'BATTY_RESTORE_READY': str(ready), 'BATTY_OFFLINE': '1'}
    processes = []
    log = open(private / 'frontend.log', 'w+')

    def launch(arguments, source=False):
        ready.unlink(missing_ok=True)
        p = subprocess.Popen([str(ROOT / 'kilix'), *arguments], env=env | {'BATTY_RESTORE_SOURCE': str(int(source))},
                             stdout=log, stderr=log)
        processes.append(p)
        wait_for(ready.exists, p)
        endpoint = ready.read_text().strip()
        wait_for(lambda: request(endpoint, 'ping'), p)
        return p, endpoint

    def stop(p):
        p.terminate()
        p.wait(timeout=5)

    try:
        source, endpoint = launch(['--session', 'restore-first', '--', '/bin/cat'], True)
        before = request(endpoint, 'checkpoint')
        panes = {p['session']: p for p in before['panes']}
        request(endpoint, 'pane-rename', panes['restore-first']['id'], 'Workspace λ'.encode())
        wait_for(lambda: request(endpoint, 'info', panes['restore-first']['id'])['panes'][0]['title'] == 'Workspace λ', source)
        request(endpoint, 'send', panes['restore-first']['id'], b'RESTORED_TEXT\n')
        wait_for(lambda: 'RESTORED_TEXT' in request(endpoint, 'dump', panes['restore-first']['id']), source)
        wait_for(lambda: any(p['session'] == 'restore-third' and p['exit_status'] == 7 for p in request(endpoint, 'list')['panes']), source)
        subprocess.run([str(ROOT / 'kilix'), 'save', '--socket', endpoint, str(saved)], env=env, check=True, timeout=5)
        before = json.loads(saved.read_text())['checkpoint']
        assert restore_records(saved)[-1] == 'DONE'
        legacy = json.loads(saved.read_text())
        current = bytes.fromhex(before['layout_hex'])
        title_at = 8 + current[5] * 10
        page_at = title_at
        for _ in range(current[5]):
            page_at += 1 + current[page_at]
        legacy_layout = b'BWL1' + current[4:title_at] + current[page_at:]
        legacy['checkpoint']['layout_format'] = 'BWL1'
        legacy['checkpoint']['layout_hex'] = legacy_layout.hex()
        legacy_file = private / 'legacy-workspace.json'
        legacy_file.write_text(json.dumps(legacy))
        assert restore_records(legacy_file)[-1] == 'DONE'
        invalid = private / 'invalid.json'
        for contents in ('{"format":"a","format":"b"}', '{"version":NaN}', '[]', '{}'):
            invalid.write_text(contents)
            try:
                restore_records(invalid)
            except ValueError:
                pass
            else:
                raise AssertionError('Invalid file accepted')
        invalid.unlink()
        invalid.symlink_to(saved)
        try:
            restore_records(invalid)
        except OSError:
            pass
        else:
            raise AssertionError('Symlink input accepted')
        invalid.unlink()
        os.mkfifo(invalid)
        try:
            restore_records(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError('FIFO input accepted')
        invalid.unlink()
        with invalid.open('wb') as stream:
            stream.truncate(1024 * 1024 + 1)
        try:
            restore_records(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError('Oversized input accepted')

        stop(source)
        old_frontend, old_endpoint = launch(['--restore', str(legacy_file)])
        old_first = next(p for p in request(old_endpoint, 'checkpoint')['panes'] if p['session'] == 'restore-first')
        assert old_first['title'] != 'Workspace λ', 'BWL1 cannot contain a pane title override'
        stop(old_frontend)
        restored, endpoint = launch(['--restore', str(saved)])
        after = request(endpoint, 'checkpoint')
        old = {p['session']: p for p in before['panes']}
        new = {p['session']: p for p in after['panes']}
        assert old.keys() == new.keys()
        for name in old:
            for field in ('pid', 'session_dir', 'session_epoch', 'observe', 'title', 'page_title', 'layout', 'active', 'tab_active', 'synchronized'):
                assert old[name][field] == new[name][field], (name, field, old[name][field], new[name][field])
        assert before['appearance'] == after['appearance'], (before['appearance'], after['appearance'])
        mapping = {new[name]['id']: old[name]['id'] for name in old}
        assert remapped_layout(after['layout_hex'], mapping) == bytes.fromhex(before['layout_hex'])
        assert 'RESTORED_TEXT' in request(endpoint, 'dump', new['restore-first']['id'])
        assert 'COMPLETED_RESTORE' in request(endpoint, 'dump', new['restore-third']['id'])
        stop(restored)
        # Valid envelope with damaged tree bytes must fail before any owner is claimed.
        bad = json.loads(saved.read_text())
        bad['checkpoint']['layout_hex'] += '00'
        corrupt = private / 'corrupt.json'
        corrupt.write_text(json.dumps(bad))
        failed = subprocess.run([str(ROOT / 'kilix'), '--restore', str(corrupt)], env=env, stdout=log, stderr=log, timeout=7)
        assert failed.returncode != 0
        restored, endpoint = launch(['--restore', str(saved)])
        assert len(request(endpoint, 'list')['panes']) == 3
        stop(restored)
        # Fail on the second descriptor after one successful attachment; cleanup
        # must release the first controller so a subsequent complete restore works.
        bad = json.loads(saved.read_text())
        bad['checkpoint']['panes'][1]['session_epoch'] = f"{int(bad['checkpoint']['panes'][1]['session_epoch'], 16) ^ 1:016x}"
        corrupt.write_text(json.dumps(bad))
        failed = subprocess.run([str(ROOT / 'kilix'), '--restore', str(corrupt)], env=env, stdout=log, stderr=log, timeout=7)
        assert failed.returncode != 0
        restored, endpoint = launch(['--restore', str(saved)])
        assert {p['session']: p['pid'] for p in request(endpoint, 'list')['panes']} == {name: p['pid'] for name, p in old.items()}
        stop(restored)
        print('PASS Kilix save/restore: surviving PIDs, layouts, appearance, completed output, malformed file and partial stale-owner cleanup')
    except Exception:
        log.flush(); log.seek(0); print(log.read(), file=sys.stderr)
        raise
    finally:
        for p in processes:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill(); p.wait()
        log.close()
        for name in ('restore-first', 'restore-second', 'restore-third'):
            subprocess.run([str(ROOT / 'batty'), '--terminate', name], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
