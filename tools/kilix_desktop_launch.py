#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run a bundled Kilix desktop flavor with Batty's terminal host."""
import argparse
import os
from pathlib import Path
import sys

from kilix_apps import inside_batty, private_directory

ROOT = Path(__file__).resolve().parent.parent
BUNDLED = ROOT / 'third_party/kilix-desktop/src'


def main():
    parser = argparse.ArgumentParser(description=__doc__, prog='kilix desktop')
    parser.add_argument('provider', nargs='?', choices=('95', 'kilix-95', 'xp', 'kilix-xp'),
                        default='95', help='Bundled desktop flavor (default: 95)')
    parser.add_argument('--source', default=os.environ.get('BATTY_KILIX_DESKTOP_SOURCE', str(BUNDLED)),
                        help='Kilix desktop source; defaults to the bundled Batty port')
    parser.add_argument('--app', help='Open a built-in desktop app at startup')
    parser.add_argument('--open', metavar='PATH', help='Open a document or directory at startup')
    parser.add_argument('--dir', help='Desktop folder override')
    args = parser.parse_args()
    flavor = 'xp' if args.provider in ('xp', 'kilix-xp') else '95'
    source = Path(args.source).expanduser().resolve(strict=True)
    if not all(path.is_file() for path in
               (source / 'desktop/main.py', source / 'config/gfx.py',
                source / 'config/kilix_sdk/__init__.py')):
        parser.error('--source must contain a Kilix desktop and its host SDK')
    presenter = source / 'third_party/kitty-frame-presenter/src/kitty_frame_presenter'
    if not presenter.is_dir():
        parser.error('--source is missing the kitty-frame-presenter module')
    bridge = ROOT / 'batty-kitten'
    child = [sys.executable, '-B', str(source / 'desktop/main.py')]
    if args.app:
        child += ['--app', args.app]
    if args.open:
        child += ['--open', str(Path(args.open).expanduser().resolve(strict=True))]
    if args.dir:
        child += ['--dir', args.dir]
    data_home = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    storage = private_directory(os.environ.get('BATTY_KILIX_STORAGE_HOME',
                                               data_home / 'batty/kilix'))
    overrides = {'KILIX_HOME': str(source), 'KILIX_KITTEN': str(bridge),
                 'KITTY_LISTEN_ON': 'batty', 'KILIX_STORAGE_HOME': storage,
                 'KILIX_DATA_HOME': str(Path(storage) / 'data'),
                 'KILIX_CONFIG_HOME': str(Path(storage) / 'config'),
                 'KILIX_CACHE_HOME': str(Path(storage) / 'cache'),
                 'KILIX_STATE_DIRECTORY': str(Path(storage) / 'state'),
                 'KILIX_SESSION_HOME': str(Path(storage) / 'session'),
                 'KILIX_DESKTOP_FLAVOR': flavor,
                 'KILIX_DESKTOP_DIR': str(Path(storage) / 'data' /
                                          ('desktop-xp' if flavor == 'xp' else 'desktop'))}
    if source == BUNDLED.resolve():
        overrides['BATTY_KILIX_DESKTOP'] = '1'
    if inside_batty():
        command = [str(ROOT / 'kilix'), 'launch', '--type=tab', '--self',
                   '--tab-title', 'kilix XP' if flavor == 'xp' else 'kilix 95']
        for name, value in overrides.items():
            command += ['--env', f'{name}={value}']
        command += ['--', '/usr/bin/env', '-u', 'KILIX_RC_PASSWORD_FILE', *child]
        os.execv(command[0], command)
        return
    environment = os.environ.copy()
    environment.update(overrides)
    environment.pop('KILIX_RC_PASSWORD_FILE', None)
    os.execve(str(ROOT / 'kilix'), [str(ROOT / 'kilix'), '--', *child], environment)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print(f'kilix desktop: {error}', file=sys.stderr)
        sys.exit(1)
