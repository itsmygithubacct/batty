#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Install the Batty Kilix desktop identity in the user's XDG directories."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

ROOT = Path(__file__).resolve().parent.parent
FILES = ('applications/batty-kilix.desktop', 'icons/hicolor/scalable/apps/batty-kilix.svg')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_file(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ValueError(f'Not a bounded regular file: {path}')
        data = source.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError(f'File is too large: {path}')
        return data


def atomic_write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.batty-desktop-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as target:
            os.fchmod(target.fileno(), mode)
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def exec_argument(value):
    # Desktop Entry string decoding precedes Exec argument decoding.
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Desktop launcher paths cannot contain control characters')
    quoted = ''.join('\\' + c if c in '\\"`$' else c for c in value)
    return '"' + quoted.replace('\\', '\\\\').replace('%', '%%') + '"'


def data_files(root):
    entry = ('[Desktop Entry]\nType=Application\nName=Kilix — Batty\n'
             'Comment=Terminal workspaces powered by Batty\n'
             f'Exec=/bin/sh {exec_argument(str(root / "kilix"))}\n'
             'Icon=batty-kilix\nTerminal=false\nCategories=System;TerminalEmulator;\n'
             'Keywords=terminal;shell;panes;\nStartupNotify=false\nStartupWMClass=batty-kilix\n')
    return dict(zip(FILES, (entry.encode(), (root / 'assets/batty-kilix.svg').read_bytes())))


def xdg_path(key, fallback):
    path = Path(os.environ.get(key) or fallback).expanduser()
    if not path.is_absolute():
        raise ValueError(f'{key} must be an absolute directory')
    return path


def update(install, root=ROOT):
    data_home = xdg_path('XDG_DATA_HOME', Path.home() / '.local/share')
    state_home = xdg_path('XDG_STATE_HOME', Path.home() / '.local/state') / 'batty'
    state_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = state_home / 'kilix-desktop.json'
    lock = os.open(state_home / 'kilix-desktop.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock, 'w') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        raw = read_file(manifest)
        recorded = json.loads(raw) if raw is not None else None
        if recorded is not None:
            if (not isinstance(recorded, dict) or recorded.get('version') != 1
                    or not isinstance(recorded.get('files'), dict) or set(recorded['files']) != set(FILES)
                    or not isinstance(recorded.get('data_home'), str)
                    or not Path(recorded['data_home']).is_absolute()):
                raise ValueError('Invalid desktop ownership record')
            for hashes in recorded['files'].values():
                if not isinstance(hashes, list) or not hashes or any(
                        not isinstance(h, str) or len(h) != 64 or any(c not in '0123456789abcdef' for c in h)
                        for h in hashes):
                    raise ValueError('Invalid desktop ownership digest')
            if install and Path(recorded['data_home']) != data_home:
                raise ValueError('Desktop data directory changed; uninstall the existing launcher first')
            data_home = Path(recorded['data_home'])
        elif not install:
            return 'No Batty Kilix desktop installation recorded.'
        contents = data_files(root) if install else {}
        # Check every target before changing any. Never follow target symlinks.
        for relative in FILES:
            existing = read_file(data_home / relative)
            if existing is not None and (recorded is None or digest(existing) not in recorded['files'][relative]):
                raise ValueError(f'Preserving unowned or modified file: {data_home / relative}')
        if install:
            hashes = {name: [digest(contents[name])] for name in FILES}
            # Journal both old and intended bytes before writes. An interrupted
            # install can be retried or uninstalled without adopting other files.
            pending = {name: sorted(set(hashes[name] + (recorded['files'][name] if recorded else []))) for name in FILES}
            record = dict(version=1, data_home=str(data_home), files=pending)
            atomic_write(manifest, (json.dumps(record) + '\n').encode(), 0o600)
            for name, content in contents.items():
                atomic_write(data_home / name, content)
            record['files'] = hashes
            atomic_write(manifest, (json.dumps(record) + '\n').encode(), 0o600)
            return f'Installed Kilix — Batty: {data_home / FILES[0]}'
        for relative in FILES:
            (data_home / relative).unlink(missing_ok=True)
        manifest.unlink()
        return 'Uninstalled the Batty Kilix desktop entry and icon.'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'uninstall'))
    args = parser.parse_args()
    try:
        print(update(args.action == 'install'))
    except (OSError, ValueError, TypeError) as error:
        parser.exit(1, f'kilix desktop: {error}\n')


if __name__ == '__main__':
    main()
