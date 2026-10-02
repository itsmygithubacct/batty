#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Workspace saves keep committed output intact across concurrent mutations."""
import copy
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import kilix_workspace as workspace
from kilix_auto_workspace import remove_snapshot
from kilix_recovery import HEADER, read

EPOCH = '0000000000000001'


def checkpoint(root):
    return {
        'version': 1, 'layout_format': 'BWL1',
        'layout_hex': (b'BWL1\x01\x01\0\0' + struct.pack('<QBB', 1, 0, 0)).hex(),
        'appearance': {'width': 800, 'height': 600, 'font': 'monospace',
                       'font_size': 16, 'pane_buttons': 511, 'chrome': True,
                       'bottom_bar': False, 'start_badge': True},
        'panes': [{'id': 1, 'persistent': True, 'observe': False, 'session': 'saved',
                   'session_dir': str(root), 'session_epoch': EPOCH}],
    }


def archive(text):
    scene = bytearray(860)
    struct.pack_into('<8sIIQQIIII', scene, 0, b'BTPRES01', 1, len(scene), 1, 1, 80, 24, 8, 16)
    command, cwd = b'/bin/sh\0', b'/tmp'
    total = HEADER.size + len(text) + len(scene) + len(command) + len(cwd)
    return HEADER.pack(b'BTRCV001', 1, total, len(text), len(scene), len(command),
                       len(cwd), 80, 24, 8, 16) + text + scene + command + cwd


def output_for(path):
    pane = workspace.restore_records(path, return_document=True)['panes'][0]
    output = path.parent / pane['recovery_output']
    assert read(output, output.stem)['epoch'] == EPOCH
    return output


def concurrent(first, second, reached, release):
    """Let a second mutation overlap the first one's publication/cleanup gap."""
    errors = []
    second_done = threading.Event()

    def run(operation, done=None):
        try:
            operation()
        except BaseException as error:
            errors.append(error)
        finally:
            if done:
                done.set()

    one = threading.Thread(target=run, args=(first,), name='first-writer')
    two = threading.Thread(target=run, args=(second, second_done), name='second-writer')
    one.start()
    try:
        assert reached.wait(3), 'First mutation did not reach its cleanup gap'
        two.start()
        # Without transaction locking the second writer commits here, then its
        # output is removed by the paused first mutation. With locking it waits.
        second_done.wait(0.25)
    finally:
        release.set()
        one.join(3)
        if two.ident is not None:
            two.join(3)
    assert not one.is_alive() and not two.is_alive(), 'Concurrent storage operation stalled'
    assert not errors, errors


with tempfile.TemporaryDirectory(prefix='bt-workspace-storage-') as directory:
    root = Path(directory)
    saved = root / 'workspace.json'
    state = checkpoint(root)
    older, newer = archive(b'older output'), archive(b'newer output')
    original_glob, original_unlink = Path.glob, Path.unlink

    reached, release = threading.Event(), threading.Event()

    def paused_glob(path, pattern):
        if threading.current_thread().name == 'first-writer':
            reached.set()
            assert release.wait(3)
        return original_glob(path, pattern)

    with patch.object(Path, 'glob', paused_glob):
        concurrent(lambda: workspace.save(saved, state, {EPOCH: older}),
                   lambda: workspace.save(saved, state, {EPOCH: newer}), reached, release)
    assert output_for(saved).read_bytes() == newer
    assert len(list(root.glob('.batty-output-*/*.bt-output'))) == 1

    reached, release = threading.Event(), threading.Event()

    def paused_unlink(path, *args, **kwargs):
        result = original_unlink(path, *args, **kwargs)
        if path == saved and threading.current_thread().name == 'first-writer':
            reached.set()
            assert release.wait(3)
        return result

    with patch.object(Path, 'unlink', paused_unlink):
        concurrent(lambda: remove_snapshot(saved),
                   lambda: workspace.save(saved, state, {EPOCH: older}), reached, release)
    assert output_for(saved).read_bytes() == older

    previous = saved.read_bytes()
    original_replace = os.replace

    def fail_manifest(source, target):
        if Path(target) == saved:
            raise OSError('Simulated manifest publication failure')
        return original_replace(source, target)

    with patch.object(os, 'replace', fail_manifest):
        try:
            workspace.save(saved, state, {EPOCH: newer})
        except OSError:
            pass
        else:
            raise AssertionError('Failed publication reported success')
    assert saved.read_bytes() == previous and output_for(saved).read_bytes() == older
    assert not list(root.glob('.batty-workspace-*'))
    workspace.save(saved, state, {EPOCH: newer})
    assert output_for(saved).read_bytes() == newer
    assert len(list(root.glob('.batty-output-*/*.bt-output'))) == 1

    # A controller and observer of one owner must describe the same captured
    # instant, even if that owner's output advances while the workspace saves.
    shared = copy.deepcopy(state)
    shared['layout_hex'] = (b'BWL1\x01\x02\0\0' + struct.pack('<QBBQBB', 1, 0, 0, 2, 0, 0)).hex()
    shared['panes'].append(shared['panes'][0] | {'id': 2, 'observe': True})
    with patch.object(workspace, 'capture', side_effect=[older, newer]):
        workspace.save(saved, shared)
    panes = workspace.restore_records(saved, return_document=True)['panes']
    assert panes[0]['recovery_output'] == panes[1]['recovery_output']
    assert output_for(saved).read_bytes() == older
    assert len(list(root.glob('.batty-output-*/*.bt-output'))) == 1
    remove_snapshot(saved)
    assert not saved.exists() and not list(root.glob('.batty-output-*'))

print('PASS workspace storage: concurrent saves/removal, failed publication, pruning and shared-owner capture')
