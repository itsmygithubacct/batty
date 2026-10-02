#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Save a Batty workspace's layout, owner references and durable output."""
import argparse
import json
import os
import re
import struct
from pathlib import Path
import stat
import sys
import tempfile
import copy
import secrets
import hashlib

from control import request
from control_paths import resolve_endpoint
from kilix_recovery import capture, private_directory, read, write, owner_missing, recovery_directory


def closed_owner(epoch, directory):
    return directory is not None and (directory / ('closed-' + epoch)).exists()


def automatic_records(path):
    records = restore_records(path)
    try:
        directory = recovery_directory()
    except (OSError, ValueError):
        directory = None
    result = records[:10]
    for index in range(int(records[9])):
        pane = records[10 + index * 5:15 + index * 5]
        if not closed_owner(pane[3], directory):
            result.extend(pane)
    result[9] = str((len(result) - 10) // 5)
    return result + ['DONE']


def save(path, checkpoint, archives=None):
    panes = checkpoint.get('panes', [])
    if not panes or any(not p.get('persistent') or not p.get('session') or
                        not p.get('session_dir') or not int(p.get('session_epoch', '0'), 16)
                        for p in panes):
        raise ValueError('Saving requires every pane to use a named persistent session')
    path = Path(path).expanduser().absolute()
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid()):
        raise ValueError('Workspace destination must be a regular file owned by the current user')
    checkpoint = copy.deepcopy(checkpoint)
    total, captured = 0, []
    for pane in checkpoint['panes']:
        archive = archives.get(pane['session_epoch']) if archives is not None else None
        if archive is None:
            archive = capture(pane)
        total += len(archive)
        if total > 256 * 1024 * 1024:
            raise ValueError('Workspace output exceeds the 256 MiB limit')
        captured.append(archive)
    output = private_directory(path.parent / ('.batty-output-' + hashlib.sha256(path.name.encode()).hexdigest()[:24]), create=True)
    for pane, archive in zip(checkpoint['panes'], captured):
        pane['recovery_output'] = output.name + '/' + write(output, archive)
    data = (json.dumps({'format': 'batty-workspace', 'version': 2,
                        'checkpoint': checkpoint}, ensure_ascii=True, indent=2) + '\n').encode()
    if len(data) > 1024 * 1024:
        raise ValueError('Workspace file exceeds the 1 MiB limit')
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.batty-workspace-', dir=path.parent)
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        os.fsync(directory)
    finally:
        if temporary is not None:
            os.unlink(temporary)
        os.close(directory)
    retained = {pane['recovery_output'].split('/')[1] for pane in checkpoint['panes']}
    for old in output.glob('*.bt-output'):
        if old.name not in retained:
            old.unlink()


def restore_records(path, *, return_document=False):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate workspace JSON key')
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError('Invalid JSON number')

    fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        st = os.fstat(stream.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > 1024 * 1024:
            raise ValueError('Workspace input must be an owned regular file of at most 1 MiB')
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError('Workspace file exceeds the 1 MiB limit')
    try:
        document = json.loads(data.decode('utf-8'), object_pairs_hook=unique,
                              parse_constant=invalid_constant)
        if document['format'] != 'batty-workspace' or type(document['version']) is not int or document['version'] not in (1, 2):
            raise ValueError('Unsupported workspace format')
        checkpoint = document['checkpoint']
        if (type(checkpoint['version']) is not int or checkpoint['version'] != 1 or
                checkpoint['layout_format'] not in ('BWL1', 'BWL2')):
            raise ValueError('Unsupported checkpoint format')
        hex_layout = checkpoint['layout_hex']
        if not isinstance(hex_layout, str) or not re.fullmatch(r'[0-9a-fA-F]{16,65536}', hex_layout) or len(hex_layout) % 2:
            raise ValueError('Invalid layout encoding')
        raw = bytes.fromhex(hex_layout)
        if (raw[:4] != checkpoint['layout_format'].encode() or
                not 1 <= raw[5] <= 64 or len(raw) < 8 + raw[5] * 10):
            raise ValueError('Invalid layout header')
        ids = [struct.unpack_from('<Q', raw, 8 + i * 10)[0] for i in range(raw[5])]
        panes = checkpoint['panes']
        if not isinstance(panes, list) or len(panes) != len(ids) or len(set(ids)) != len(ids) or 0 in ids:
            raise ValueError('Invalid saved pane membership')
        a = checkpoint['appearance']
        for key, low, high in (('width', 120, 16384), ('height', 80, 16384), ('font_size', 6, 96), ('pane_buttons', 0, 511)):
            if type(a[key]) is not int or not low <= a[key] <= high:
                raise ValueError('Invalid saved appearance')
        if not isinstance(a['font'], str) or not 1 <= len(a['font'].encode('utf-8')) <= 1024 or any(ord(c) < 32 for c in a['font']):
            raise ValueError('Invalid saved font')
        for key in ('chrome', 'bottom_bar', 'start_badge'):
            if type(a[key]) is not bool:
                raise ValueError('Invalid saved chrome option')
        records = [str(a['width']), str(a['height']), a['font'], str(a['font_size']),
                   str(int(a['chrome'])), str(int(a['bottom_bar'])), str(int(a['start_badge'])),
                   str(a['pane_buttons']), hex_layout, str(len(panes))]
        seen = set()
        for p in panes:
            if type(p['id']) is not int or p['id'] not in ids or p['id'] in seen or p['persistent'] is not True or type(p['observe']) is not bool:
                raise ValueError('Invalid saved persistent pane')
            seen.add(p['id'])
            root, name, epoch = p['session_dir'], p['session'], p['session_epoch']
            if not isinstance(root, str) or not root.startswith('/') or not 1 < len(root.encode('utf-8')) < 4096 or '\0' in root or any(part in ('.', '..') for part in root.split('/')):
                raise ValueError('Invalid saved session directory')
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,47}', name):
                raise ValueError('Invalid saved session name')
            if not isinstance(epoch, str) or not re.fullmatch(r'[0-9a-fA-F]{16}', epoch) or not int(epoch, 16):
                raise ValueError('Invalid saved session epoch')
            output = p.get('recovery_output')
            if output is not None and (not isinstance(output, str) or not re.fullmatch(
                    r'[A-Za-z0-9_.-]{1,255}/[0-9a-f]{64}\.bt-output', output) or
                    output.split('/')[0] in ('.', '..')):
                raise ValueError('Invalid saved output path')
            records += [str(p['id']), root, name, epoch, str(int(p['observe']))]
        return checkpoint if return_document else records + ['DONE']
    except (KeyError, TypeError, RecursionError, struct.error) as error:
        raise ValueError('Malformed workspace file') from error


def main():
    if len(sys.argv) in (4, 5) and sys.argv[1] == '_recover':
        try:
            checkpoint = restore_records(sys.argv[2], return_document=True)
            pane = next(p for p in checkpoint['panes'] if p['id'] == int(sys.argv[3]))
            if not owner_missing(pane):
                raise ValueError('Saved owner is still alive; attachment must succeed before continuing')
            output = pane.get('recovery_output')
            if not output:
                raise ValueError('This legacy workspace has no durable output archive')
            path = Path(sys.argv[2]).expanduser().absolute().parent / output
            private_directory(path.parent)
            metadata = read(path, path.stem)
            if metadata['epoch'] != pane['session_epoch']:
                raise ValueError('Saved output belongs to another owner')
            records = [str(path), metadata['cwd'], 'kilix-auto-' + secrets.token_hex(12)]
            command = metadata['argv'] if sys.argv[4:] == ['--restart-programs'] else []
            records += [str(len(command)), *command, 'DONE']
            sys.stdout.buffer.write(('\0'.join(records) + '\0').encode('utf-8'))
            return 0
        except (OSError, RuntimeError, ValueError, StopIteration) as error:
            print(f'kilix recovery: {error}', file=sys.stderr)
            return 1
    if len(sys.argv) == 3 and sys.argv[1] in ('_restore', '_restore-auto'):
        try:
            records = automatic_records(sys.argv[2]) if sys.argv[1] == '_restore-auto' else restore_records(sys.argv[2])
            sys.stdout.buffer.write(('\0'.join(records) + '\0').encode('utf-8'))
            return 0
        except (OSError, RuntimeError, ValueError) as error:
            print(f'kilix restore: {error}', file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', help='Explicit endpoint; otherwise discover the calling pane')
    parser.add_argument('file', help='Save file; its parent directory must exist')
    args = parser.parse_args()
    try:
        checkpoint = request(resolve_endpoint(args.socket), 'checkpoint')
        save(args.file, checkpoint)
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f'kilix save: {error}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
