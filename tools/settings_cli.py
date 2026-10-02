#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Inspect and update supported shared GPU Terminal settings."""
import argparse
import fcntl
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from kilix_settings import BUTTONS, LIMIT, FALSE, TRUE, TRANSCRIPTS, read_document, read_settings, settings_path, transcript_value

KEYS = {'tab_bar_edge': 'KILIX_CHROME_TAB_BAR_EDGE', 'start_menu': 'KILIX_CHROME_START_MENU'} | {
    'button_' + name.lower(): 'KILIX_CHROME_BUTTON_' + name for name in BUTTONS} | {
    name: spec[0] for name, spec in TRANSCRIPTS.items()}


def setting_key(name):
    if name in KEYS:
        return KEYS[name]
    if name in KEYS.values():
        return name
    raise ValueError(f'Unsupported setting: {name}')


def normalize(key, value):
    value = value.strip().lower()
    for name, (stored, _, choices) in TRANSCRIPTS.items():
        if key == stored:
            if name == 'transcript' and value in FALSE + TRUE:
                return 'off' if value in FALSE else 'on'
            for choice in choices:
                if value == choice.lower():
                    return choice
            raise ValueError(f'Invalid value for {key}: {value}')
    if key == KEYS['tab_bar_edge']:
        if value in ('top', 'bottom'):
            return value
    elif value in FALSE + TRUE:
        return 'off' if value in FALSE else 'on'
    raise ValueError(f'Invalid value for {key}: {value}')


def update(path, changes):
    # Match the shared SDK lock filename so writers from either host serialize.
    changes = {setting_key(key): normalize(setting_key(key), value) for key, value in changes.items()}
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path) + '.lock', os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600)
    temporary = None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('Shared settings lock must be a regular file')
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = read_document(path)
        for key, value in changes.items():
            line = (key + '=' + value).encode()
            pattern = re.compile(rb'^[ \t]*' + key.encode() + rb'=[^\r\n]*', re.MULTILINE)
            matches = list(pattern.finditer(data))
            if matches:
                last = matches[-1]
                data = data[:last.start()] + line + data[last.end():]
            else:
                data += (b'\n' if data and not data.endswith(b'\n') else b'') + line + b'\n'
        if len(data) > LIMIT:
            raise ValueError('Updated shared settings exceed 1 MiB')
        out_fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
        with os.fdopen(out_fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            os.unlink(temporary)
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument('--get', metavar='KEY')
    commands.add_argument('--set', action='append', metavar='KEY=VALUE')
    commands.add_argument('--list', action='store_true')
    args = parser.parse_args()
    path = settings_path()
    if args.set is not None:
        changes = {}
        for assignment in args.set:
            name, separator, value = assignment.partition('=')
            if not separator:
                parser.error('--set requires KEY=VALUE')
            key = setting_key(name)
            changes[key] = normalize(key, value)
        update(path, changes)
        print(f'Updated {path}; Ctrl+Shift+F5 reloads presentation settings. Recording settings apply in new windows; retention commands read them on each run.')
        return
    values = read_settings(path)
    def value(key):
        for name, spec in TRANSCRIPTS.items():
            if key == spec[0]:
                return transcript_value(values, name)
        raw = values.get(key)
        if key == KEYS['tab_bar_edge']:
            return raw.lower() if raw and raw.lower() in ('top', 'bottom') else 'top'
        if raw is None and key == KEYS['start_menu']:
            return 'off'
        return 'off' if raw is not None and raw.lower() in FALSE else 'on'
    if args.get:
        print(value(setting_key(args.get)))
    else:
        for name, key in KEYS.items():
            print(name + '=' + value(key))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print(f'kilix settings: {error}', file=sys.stderr)
        sys.exit(1)
