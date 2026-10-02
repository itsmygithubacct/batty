#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Store the desktop selected for future bare Batty Kilix launches."""
import os
from pathlib import Path
import stat
import sys
import tempfile

from kilix_apps import private_directory

CHOICES = ('auto', 'builtin', 'external', '95', 'xp', 'tui', 'cap', 'land', 'icewm', 'none')


def setting_path():
    data = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    storage = Path(os.environ.get('BATTY_KILIX_STORAGE_HOME') or data / 'batty/kilix').expanduser()
    if not storage.is_absolute():
        raise ValueError('Batty Kilix storage directory must be absolute')
    return Path(private_directory(Path(private_directory(storage)) / 'config')) / 'default-desktop'


def current():
    path = setting_path()
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return 'none'
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('desktop selection must be a private regular file')
        raw = stream.read(65)
    if len(raw) > 64:
        raise ValueError('desktop selection is too long')
    value = raw.decode('ascii').strip()
    if value not in CHOICES:
        raise ValueError('desktop selection is invalid')
    return value


def select(value):
    if value not in CHOICES:
        raise ValueError('desktop provider is not ported to Batty')
    path = setting_path()
    fd, temporary = tempfile.mkstemp(prefix='.default-desktop-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write((value + '\n').encode('ascii'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args in ([], ['show']):
        print(current())
        return 0
    if args == ['list']:
        print('\n'.join(CHOICES))
        return 0
    if args == ['_startup']:
        choice = current()
        print(choice if choice in ('none', 'xp', 'tui', 'cap', 'land', 'icewm') else '95')
        return 0
    if len(args) == 2 and args[0] == 'set':
        select(args[1])
        print(f'default desktop: {args[1]}')
        return 0
    raise ValueError('usage: kilix default-desktop [show|list|set NAME]')


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError) as error:
        print(f'kilix default-desktop: {error}', file=sys.stderr)
        raise SystemExit(1)
