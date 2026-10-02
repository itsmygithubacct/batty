#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Expose the bundled desktop's pinned game catalog through Batty."""
import os
from pathlib import Path
import sys

from kilix_apps import private_directory
from kilix_settings import settings_path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'third_party/kilix-desktop/src'


def backend():
    data_home = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    storage = Path(private_directory(os.environ.get('BATTY_KILIX_STORAGE_HOME',
                                                    data_home / 'batty/kilix')))
    os.environ.update(KILIX_HOME=str(SOURCE), KILIX_STORAGE_HOME=str(storage),
                      KILIX_DATA_HOME=str(storage / 'data'),
                      KILIX_CONFIG_HOME=str(storage / 'config'),
                      KILIX_CACHE_HOME=str(storage / 'cache'),
                      KILIX_STATE_DIRECTORY=str(storage / 'state'),
                      KILIX_SESSION_HOME=str(storage / 'session'))
    sys.path.insert(0, str(SOURCE / 'config'))
    sys.path.insert(0, str(SOURCE / 'desktop'))
    import games
    from kilix_sdk import settings
    return games, settings


def usage():
    return 'usage: kilix games [list|settings|install GAME|play GAME [--setup-only]|enable GAME...|disable GAME...]'


def main(args=None):
    args = list(sys.argv[1:] if args is None else args)
    action = args.pop(0) if args else 'list'
    if action in ('help', '-h', '--help'):
        print(usage())
        return 0
    if action not in ('list', 'settings', 'install', 'play', 'enable', 'disable'):
        raise ValueError(usage())
    games, shared = backend()
    if action == 'list':
        if args:
            raise ValueError(usage())
        availability = shared.game_availability(str(settings_path()))
        for game_id, label in shared.GAME_TOGGLE_IDS:
            print(f'{game_id} {"enabled" if availability[game_id] else "disabled"} {label}')
        return 0
    if action == 'settings':
        if args:
            raise ValueError(usage())
        os.execv(str(ROOT / 'kilix'), [str(ROOT / 'kilix'), 'desktop', '--app', 'settings'])
    if action in ('enable', 'disable'):
        if not args or any(game_id not in shared.GAME_KEY_BY_ID for game_id in args):
            raise ValueError(usage())
        shared.update({shared.GAME_KEY_BY_ID[game_id]:
                       'on' if action == 'enable' else 'off' for game_id in args},
                      str(settings_path()))
        return main(['list'])
    setup_only = '--setup-only' in args
    args = [arg for arg in args if arg != '--setup-only']
    if len(args) != 1 or args[0] not in shared.GAME_KEY_BY_ID:
        raise ValueError(usage())
    game_id = args[0]
    if action == 'install':
        if game_id not in games.DESKTOP_APP_GAMES:
            games.ensure(game_id, report=lambda message: print(
                f'kilix games: {message}', file=sys.stderr))
        return 0
    if not shared.game_enabled(game_id, str(settings_path())):
        raise ValueError(f'{game_id} is disabled; run kilix games enable {game_id}')
    if game_id in games.DESKTOP_APP_GAMES:
        if setup_only:
            return 0
        app = games.DESKTOP_APP_GAMES[game_id][0]
        os.execv(str(ROOT / 'kilix'), [str(ROOT / 'kilix'), 'desktop', '--app', app])
    payload = games.ensure(game_id, report=lambda message: print(
        f'kilix games: {message}', file=sys.stderr))
    if setup_only:
        return 0
    if game_id == 'doom':
        dosbox, config, executable = payload
        command = [str(ROOT / 'kilix'), 'run', '--fill', '--size', '640x400', '--',
                   dosbox, '-conf', config, executable, '-exit']
    elif game_id == 'dosbox':
        dosbox, config = payload
        command = [str(ROOT / 'kilix'), 'run', '--fill', '--size', '640x400', '--',
                   dosbox, '-conf', config, '-c', f'mount c "{games.GAMES_DIR}"', '-c', 'c:']
    else:
        command = [payload]
    os.execv(command[0], command)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(f'kilix games: {error}', file=sys.stderr)
        raise SystemExit(1)
