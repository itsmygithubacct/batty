#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Persist and apply the Batty Kilix workspace font size."""
import argparse
import os
import re
import stat
import sys
import tempfile

from control import request
from control_paths import frontend_endpoints
from kilix_default_desktop import setting_path

DEFAULT = 16


def config_path():
    return setting_path().parent / 'font-size'


def current():
    path = config_path()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return DEFAULT
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                info.st_mode & 0o077 or info.st_size > 8):
            raise ValueError('Font setting must be a small, private owned regular file')
        raw = stream.read(9)
    if not re.fullmatch(rb'(?:[6-9]|[1-8][0-9]|9[0-6])\n', raw):
        raise ValueError('Font setting must contain an integer from 6 to 96')
    return int(raw)


def persist(size):
    path = config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.stat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
        raise ValueError('Font setting directory must be private and owned by this user')
    fd, name = tempfile.mkstemp(prefix='.kilix-font-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(f'{size}\n'.encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def apply(size, explicit):
    endpoints = set()
    if explicit:
        endpoints.add(explicit)
    try:
        endpoints.update(path for _, path, _ in frontend_endpoints())
    except FileNotFoundError:
        pass
    failures = []
    for path in sorted(endpoints):
        try:
            if 'font-size' not in request(path, 'ping')['operations']:
                raise RuntimeError('Running frontend does not support font-size control')
            request(path, 'font-size', payload=bytes((size,)))
        except (OSError, RuntimeError, ValueError, KeyError) as error:
            failures.append(f'{path}: {error}')
    if failures:
        raise RuntimeError('Saved font size, but could not update all running windows:\n' + '\n'.join(failures))


def main(argv=None):
    if (argv if argv is not None else sys.argv[1:]) == ['--default']:
        print(current())
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', help='Also update this explicit workspace endpoint')
    parser.add_argument('action', nargs='?', default='show')
    parser.add_argument('value', nargs='?')
    args = parser.parse_args(argv)
    before = current()
    action = args.action
    if action in ('show', 'status', 'get'):
        if args.value is not None:
            parser.error('show takes no value')
        print(f'font_size {before}')
        return 0
    if action in ('reset', 'default'):
        if args.value is not None:
            parser.error('reset takes no value')
        size = DEFAULT
    elif action in ('larger', 'increase', 'up', 'plus', 'smaller', 'decrease', 'down', 'minus'):
        step = args.value if args.value is not None else '2'
        if not re.fullmatch(r'[0-9]+', step):
            parser.error('step must be a nonnegative integer')
        size = before + int(step) * (1 if action in ('larger', 'increase', 'up', 'plus') else -1)
    elif action == 'set':
        if args.value is None:
            parser.error('set requires a size')
        size = args.value
    elif args.value is None and re.fullmatch(r'[+-]?[0-9]+', action):
        size = before + int(action) if action[0] in '+-' else action
    else:
        parser.error('expected show, larger, smaller, reset, set SIZE, SIZE, +STEP, or -STEP')
    if not re.fullmatch(r'[0-9]+', str(size)) or not 6 <= int(size) <= 96:
        parser.error('font size must be an integer from 6 to 96 pixels')
    size = int(size)
    persist(size)
    apply(size, args.socket or os.environ.get('BATTY_CONTROL'))
    print(f'font_size {size}')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(f'kilix screen-size: {error}', file=sys.stderr)
        raise SystemExit(1)
