#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Inspect Batty's owner-written PTY transcripts."""
import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys

import transcript_storage as storage
from control import request
from control_paths import resolve_endpoint
from kilix_settings import read_settings, settings_path, transcript_value

NAME = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,47}\Z', re.ASCII)
MAX_LOG = 128 * 1024 * 1024
MAX_META = 128 * 1024


def directory():
    return os.environ.get('BATTY_TRANSCRIPT_DIR') or str(
        Path(os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local/state')) / 'batty/transcripts')


def open_root(path):
    if not path.startswith('/') or any(p in ('.', '..') for p in path.split('/')):
        raise ValueError('Transcript directory must be absolute without . or .. components')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in filter(None, path.split('/')):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('Transcript directory must be user-owned with mode 0700')
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_file(root, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=root)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError(f'Unsafe transcript file: {name}')
        if info.st_size > limit:
            raise ValueError(f'Transcript file exceeds its size limit: {name}')
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def decode_bytes(value):
    if not isinstance(value, str):
        raise ValueError('Invalid transcript metadata string')
    return value.encode('latin1').decode('utf-8', errors='replace')


def metadata(root, identity):
    try:
        fd, _ = open_file(root, identity + '.meta', MAX_META)
    except FileNotFoundError:
        return {'state': 'unknown'}
    try:
        live = False
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            live = True
        data = os.pread(fd, MAX_META + 1, 0)
        if len(data) > MAX_META:
            raise ValueError('Transcript metadata grew past its size limit')
        lines = data.splitlines()
        # A live worker may not have finished its initial metadata write.
        if live and (not lines or not lines[0].endswith(b'}')):
            return {'state': 'recording'}
        if not 1 <= len(lines) <= 2:
            raise ValueError('Invalid transcript metadata record count')
        first = json.loads(lines[0])
        if (not isinstance(first, dict) or first.get('version') != 1 or first.get('id') != identity
                or type(first.get('started')) is not int or type(first.get('pid')) is not int
                or not isinstance(first.get('argv_bytes'), list) or len(first['argv_bytes']) > 32
                or type(first.get('truncated')) is not bool):
            raise ValueError('Invalid transcript metadata')
        result = dict(state='recording' if live else 'interrupted', started=first['started'],
                      pid=first['pid'], cwd=decode_bytes(first.get('cwd_bytes')),
                      argv=[decode_bytes(v) for v in first['argv_bytes']], truncated=first['truncated'])
        if not live and len(lines) == 2:
            last = json.loads(lines[1])
            if (not isinstance(last, dict) or type(last.get('ended')) is not int
                    or type(last.get('error')) is not int or not 0 <= last['error'] <= 4095):
                raise ValueError('Invalid transcript completion metadata')
            result.update(ended=last['ended'], error=last['error'],
                          state='complete' if last['error'] == 0 else 'interrupted' if last['error'] == errno.ECANCELED else 'failed')
        return result
    finally:
        os.close(fd)


def index(root):
    found = {}
    for location in ('', 'recent', 'archive'):
        with storage.tier(root, location) as directory:
            if directory is None:
                continue
            suffix = '.log.zst' if location else '.log'
            with os.scandir(directory) as entries:
                for entry in entries:
                    if not entry.name.endswith(suffix) or not NAME.fullmatch(entry.name[:-len(suffix)]):
                        continue
                    identity = entry.name[:-len(suffix)]
                    if identity not in found and len(found) >= 10000:
                        raise ValueError('Transcript index exceeds 10000 entries')
                    fd, info = open_file(directory, entry.name, storage.MAX_ENCODED if location else MAX_LOG)
                    os.close(fd)
                    if identity not in found:
                        found[identity] = dict(id=identity, bytes=info.st_size, modified=info.st_mtime,
                                               tier=location or 'raw', copies=[], sizes={}, **metadata(root, identity))
                    found[identity]['copies'].append(location)
                    found[identity]['sizes'][location] = info.st_size
    return sorted(found.values(), key=lambda r: (r.get('started', r['modified']), r['id']), reverse=True)


def locate(root, identity):
    for location in ('', 'recent', 'archive'):
        with storage.tier(root, location) as directory:
            if directory is None:
                continue
            try:
                fd, info = open_file(directory, storage.filename(identity, location),
                                     storage.MAX_ENCODED if location else MAX_LOG)
                return fd, info, location
            except FileNotFoundError:
                pass
    raise FileNotFoundError('No transcript with that ID')


def show(root, identity, output):
    fd, info, location = locate(root, identity)
    try:
        # Snapshot raw length before reading. Compressed files are validated and
        # decoded with an output-size limit before any bytes reach the caller.
        with storage.decoded(fd, bool(location)) as plain:
            length = os.fstat(plain).st_size if location else info.st_size
            at = 0
            while at < length:
                data = os.pread(plain, min(65536, length - at), at)
                if not data:
                    raise ValueError('Transcript changed during reading')
                output.write(data)
                at += len(data)
    finally:
        os.close(fd)


def budget(value):
    if value == 'off':
        return 0
    match = re.fullmatch(r'([0-9]+)([KMG]?)', value)
    if not match:
        raise argparse.ArgumentTypeError('Budget must be bytes, K/M/G, or off')
    size = int(match[1]) * 1024 ** ('', 'K', 'M', 'G').index(match[2])
    if size > 1024 ** 4:
        raise argparse.ArgumentTypeError('Budget exceeds 1 TiB')
    return size


def plain(value):
    return ''.join(c if c.isprintable() else ' ' for c in value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=('list', 'show', 'path', 'prune', 'archive'), default='list')
    parser.add_argument('session', nargs='?', help='Exact transcript ID from the index')
    parser.add_argument('--directory')
    parser.add_argument('--pane', type=int, help='Resolve this live pane to its owner transcript')
    parser.add_argument('--socket', help='Explicit pane-control endpoint for --pane')
    parser.add_argument('--json', action='store_true', help='Machine-readable index')
    parser.add_argument('--recent-budget', type=budget)
    parser.add_argument('--archive-budget', type=budget)
    args = parser.parse_args()
    if args.pane is not None:
        if args.pane <= 0 or args.action not in ('show', 'path') or args.session or args.directory:
            parser.error('--pane requires show/path and cannot be combined with an ID or --directory')
        panes = request(resolve_endpoint(args.socket), 'info', args.pane).get('panes')
        if not isinstance(panes, list) or len(panes) != 1 or not isinstance(panes[0], dict):
            raise ValueError('Pane control returned no matching pane')
        args.session = panes[0].get('transcript_id')
        args.directory = panes[0].get('transcript_dir')
        if not isinstance(args.session, str) or not isinstance(args.directory, str):
            raise ValueError('Pane has no available transcript identity and directory')
    elif args.socket:
        parser.error('--socket requires --pane')
    if args.session is not None and not NAME.fullmatch(args.session):
        parser.error('Invalid transcript ID')
    if args.action == 'show' and args.session is None:
        parser.error('show requires a transcript ID or --pane')
    if args.action in ('list', 'prune', 'archive') and args.session is not None:
        parser.error('This action does not accept a transcript ID')
    if args.json and args.action not in ('list', 'prune', 'archive'):
        parser.error('--json is only valid for list/prune/archive')
    if args.directory is None:
        args.directory = directory()
    fd = open_root(args.directory)
    try:
        with storage.maintenance_lock(fd, args.action in ('prune', 'archive')):
            if args.action == 'show':
                show(fd, args.session, sys.stdout.buffer)
            elif args.action == 'path':
                if args.session:
                    log, _, location = locate(fd, args.session)
                    os.close(log)
                print(os.path.join(args.directory, location, storage.filename(args.session, location)) if args.session else args.directory)
            elif args.action in ('prune', 'archive'):
                settings = read_settings(settings_path())
                if args.recent_budget is None:
                    args.recent_budget = budget(transcript_value(settings, 'transcript_total'))
                if args.archive_budget is None:
                    args.archive_budget = budget(transcript_value(settings, 'transcript_archive_total'))
                report = storage.maintain(fd, sys.modules[__name__], args.recent_budget, args.archive_budget, args.action == 'archive')
                print(json.dumps(report, sort_keys=True))
                if report['over_budget']:
                    raise ValueError('Protected transcripts prevent meeting the requested budgets')
            else:
                rows = index(fd)
                if args.json:
                    print(json.dumps(rows, ensure_ascii=True))
                else:
                    for row in rows:
                        command = plain(' '.join(row.get('argv', [])))
                        print(f"{row['id']}\t{row['state']}\t{row['bytes']}\t{command}")
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        print(f'Kilix transcript: {error}', file=sys.stderr)
        raise SystemExit(1)
