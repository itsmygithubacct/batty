#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Install and launch the pinned Kilix text desktop in a Batty page."""
import argparse
from dataclasses import replace
import os
from pathlib import Path
import sys

import kilix_content_app as app
from kilix_apps import inside_batty, private_directory

ROOT = Path(__file__).resolve().parent.parent
BUNDLED = ROOT / 'third_party/kilix-desktop/src'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, prog='kilix tui')
    parser.add_argument('--install-only', action='store_true')
    args, forwarded = parser.parse_known_args(argv)
    base = app.application_spec('kilix-file')
    if base.package_id != 'kilix-tui-utils':
        raise app.CatalogError('text desktop package differs from pinned catalog')
    spec = replace(base, content_id='kilix-tui', label='Kilix TUI',
                   binary='.runtime/bin/kilix-tui', actions=(), accepts=())
    executable = Path(app.ensure_application(spec, install=True if args.install_only else None))
    if args.install_only:
        print(executable)
        return 0
    checkout = executable.parent.parent.parent.resolve(strict=True)
    if not (checkout / 'kilix-tui/main.py').is_file():
        raise ValueError('pinned text desktop entry point is missing')
    data = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    storage = private_directory(os.environ.get('BATTY_KILIX_STORAGE_HOME', data / 'batty/kilix'))
    child = [sys.executable, '-B', str(ROOT / 'tools/kilix_tui_entry.py'),
             str(checkout), *forwarded]
    overrides = {'KILIX_HOME': str(BUNDLED), 'KILIX_KITTEN': str(ROOT / 'batty-kitten'),
                 'KITTY_LISTEN_ON': 'batty', 'KILIX_STORAGE_HOME': storage,
                 'KILIX_DATA_HOME': str(Path(storage) / 'data'),
                 'KILIX_CONFIG_HOME': str(Path(storage) / 'config'),
                 'KILIX_CACHE_HOME': str(Path(storage) / 'cache'),
                 'KILIX_STATE_DIRECTORY': str(Path(storage) / 'state'),
                 'KILIX_SESSION_HOME': str(Path(storage) / 'session'),
                 'GPU_TERMINAL_HOME': storage,
                 'GPU_TERMINAL_SOURCE_HOME': str(ROOT),
                 'KILIX_DESKTOP_PROVIDER': 'tui',
                 'PATH': str(ROOT) + os.pathsep + os.environ.get('PATH', '')}
    if forwarded[:1] in (['--status'], ['-s']) or '--screenshot' in forwarded:
        environment = os.environ.copy()
        environment.update(overrides)
        environment.pop('KILIX_RC_PASSWORD_FILE', None)
        os.execve(child[0], child, environment)
        return 0
    if inside_batty():
        command = [str(ROOT / 'kilix'), 'launch', '--type=tab', '--self',
                   '--tab-title', 'Kilix TUI']
        for name, value in overrides.items():
            command += ['--env', f'{name}={value}']
        command += ['--', '/usr/bin/env', '-u', 'KILIX_RC_PASSWORD_FILE', *child]
        os.execv(command[0], command)
        return 0
    environment = os.environ.copy()
    environment.update(overrides)
    environment.pop('KILIX_RC_PASSWORD_FILE', None)
    os.execve(str(ROOT / 'kilix'), [str(ROOT / 'kilix'), '--', *child], environment)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, app.CatalogError, app.InstallError) as error:
        print(f'kilix tui: {error}', file=sys.stderr)
        raise SystemExit(1)
