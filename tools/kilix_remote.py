#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Kilix commands backed by Batty's workspace API."""
import argparse
import json
import os
import re
import secrets
from pathlib import Path
import subprocess
import sys
import time

from control import DIRECTIONS, request
from control_paths import ancestors, frontend_endpoints, resolve_endpoint, resolve_window

ROOT = Path(__file__).resolve().parent.parent


def self_pane(panes):
    for pid in ancestors():
        matches = [p for p in panes if p['pid'] == pid and p['exit_status'] is None and not p['observe']]
        if len(matches) == 1:
            return matches[0]
    raise ValueError('Cannot identify the calling pane; supply --target PANE_ID')


def resolve(panes, target):
    kind, separator, number = target.partition(':')
    if not separator:
        kind, number = '', kind
    if kind not in ('', 'pane', 'tab') or not number.isascii() or not number.isdigit():
        raise ValueError('Target must be a pane ID, tab ID, pane:ID or tab:ID')
    identity = int(number)
    if kind != 'tab':
        for pane in panes:
            if pane['id'] == identity:
                return pane
    if kind != 'pane':
        matches = [p for p in panes if p['tab'] == identity]
        if matches:
            return next((p for p in matches if p['tab_active']), matches[0])
    raise ValueError(f'No live pane or tab matches {target}')


def current_directory(pane):
    if pane['exit_status'] is not None:
        raise ValueError('The source process has exited; supply --cwd DIRECTORY')
    try:
        return os.readlink(f'/proc/{pane["pid"]}/cwd')
    except OSError as error:
        raise ValueError('Cannot read source directory; supply --cwd DIRECTORY') from error


def managed_shell():
    binary = os.environ.get('BATTY_BASH')
    if not binary:
        binary = json.loads((ROOT / 'build/bash-os.json').read_text())['binary']
    if not os.path.isabs(binary) or not os.access(binary, os.X_OK):
        raise ValueError('Managed Bash is unavailable; build Batty first')
    return binary


def plain(value):
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    listing = sub.add_parser('ls')
    listing.add_argument('--panes', '-p', action='store_true')
    listing.add_argument('--json', action='store_true')
    listing.add_argument('--all', action='store_true', help='List every live frontend in the registry')
    sub.add_parser('focus').add_argument('target')
    sub.add_parser('reload-settings', help='Reload supported shared settings in the running frontend')
    sub.add_parser('close').add_argument('target')
    rename = sub.add_parser('rename')
    rename.add_argument('target')
    rename.add_argument('title')
    watch = sub.add_parser('watch')
    watch.add_argument('target')
    watch.add_argument('--once', action='store_true')
    watch.add_argument('--interval', '-n', type=float, default=1)
    watch.add_argument('--plain', action='store_true', help='Visible text is always plain')
    watch.add_argument('--extent', choices=('screen',), default='screen')
    for action, aliases in (('new-pane', ['split']), ('new-page', ['new-tab']), ('launch', [])):
        launch = sub.add_parser(action, aliases=aliases)
        if action == 'new-pane':
            launch.add_argument('direction', nargs='?', choices=DIRECTIONS, default='right')
        if action == 'launch':
            launch.add_argument('--type', choices=('tab', 'os-window'), default='tab')
            launch.add_argument('--self', action='store_true', help='Resolve the source from this process')
        launch.add_argument('--target', help='Explicit source pane or tab; defaults to the calling pane')
        launch.add_argument('--cwd', default='current')
        launch.add_argument('--tab-title', help='Title of the destination page')
        launch.add_argument('--keep-focus', action='store_true',
                            help='Keep the source pane focused after launch')
        launch.add_argument('--pane-title', help='Title of the created pane')
        launch.add_argument('--env', action='append', default=[], metavar='NAME=VALUE',
                            help='Set a child-only environment variable; repeat as needed')
        launch.epilog = 'Pass a child command after --.'
    arguments = sys.argv[1:]
    command = []
    if '--' in arguments:
        boundary = arguments.index('--')
        command = arguments[boundary + 1:]
        arguments = arguments[:boundary]
    for command_parser in set(sub.choices.values()):
        command_parser.add_argument('--socket', help='Explicit endpoint, including outside a pane')
        command_parser.add_argument('--window', help='Registry window ID from kilix ls --all')
    args = parser.parse_args(arguments)
    if command and args.action in ('ls', 'focus', 'close', 'rename', 'watch', 'reload-settings'):
        parser.error('This operation does not take a child command')
    if args.window and args.socket:
        parser.error('--window and --socket are mutually exclusive')
    if args.action == 'ls' and args.all:
        if args.window or args.socket:
            parser.error('--all cannot be combined with --window or --socket')
        windows = []
        for identity, endpoint, entries in frontend_endpoints():
            panes = []
            for pane in entries:
                try:
                    pane.update(request(endpoint, 'info', pane['id'])['panes'][0])
                    try: pane['cwd'] = current_directory(pane)
                    except ValueError: pane['cwd'] = None
                except (OSError, RuntimeError, KeyError, IndexError):
                    continue  # A pane can close after the registry snapshot.
                panes.append(pane)
            windows.append({'id': identity, 'panes': panes})
        if args.json:
            print(json.dumps({'version': 1, 'windows': windows}, ensure_ascii=True))
        elif args.panes:
            print('WINDOW         ACT  PANE  PAGE  PID  TITLE  CWD')
            for window in windows:
                for pane in window['panes']:
                    print(f'{window["id"]}  {"*" if pane["active"] else " "}  '
                          f'{pane["id"]}  {pane["tab"]}  {pane["pid"]}  '
                          f'{plain(pane["title"])}  {plain(pane["cwd"] or "")}')
        else:
            print('WINDOW         ACT  PAGE  PANES  TITLE')
            for window in windows:
                panes = window['panes']
                for tab in dict.fromkeys(p['tab'] for p in panes):
                    members = [p for p in panes if p['tab'] == tab]
                    active = next((p for p in members if p['tab_active']), members[0])
                    print(f'{window["id"]}  {"*" if any(p["active"] for p in members) else " "}  '
                          f'{tab}  {len(members)}  {plain(active.get("page_title") or active["title"])}')
        return
    path = resolve_window(args.window) if args.window else resolve_endpoint(args.socket)
    if args.action == 'reload-settings':
        ticket = request(path, 'reload-settings')['ticket']
        deadline = time.monotonic() + 5
        while True:
            result = request(path, 'reload-status', ticket)
            if result['done']:
                if result['status']:
                    raise RuntimeError(f'Frontend settings reload failed (status {result["status"]})')
                print('kilix reload-settings: applied supported shared settings')
                return
            if time.monotonic() >= deadline:
                raise RuntimeError('Frontend settings reload did not complete within five seconds')
            time.sleep(0.03)
    panes = request(path, 'list')['panes']
    if args.action == 'ls':
        for pane in panes:
            pane.update(request(path, 'info', pane['id'])['panes'][0])
            try: pane['cwd'] = current_directory(pane)
            except ValueError: pane['cwd'] = None
        if args.json:
            print(json.dumps({'version': 1, 'panes': panes}, ensure_ascii=True))
        elif args.panes:
            print('ACT  PANE  PAGE  PID  TITLE  CWD')
            for pane in panes:
                print(f'{"*" if pane["active"] else " "}  {pane["id"]}  {pane["tab"]}  {pane["pid"]}  '
                      f'{plain(pane["title"])}  {plain(pane["cwd"] or "")}')
        else:
            print('ACT  PAGE  PANES  TITLE')
            for tab in dict.fromkeys(p['tab'] for p in panes):
                members = [p for p in panes if p['tab'] == tab]
                active = next((p for p in members if p['tab_active']), members[0])
                print(f'{"*" if any(p["active"] for p in members) else " "}  {tab}  {len(members)}  {plain(active.get("page_title") or active["title"])}')
        return
    if args.action in ('close', 'rename'):
        pane = resolve(panes, args.target)
        if args.action == 'rename':
            title = args.title
            if len(title.encode('utf-8')) > 255 or any(
                    ord(char) < 32 or 127 <= ord(char) <= 159 or 0xd800 <= ord(char) <= 0xdfff
                    for char in title):
                parser.error('Titles must be printable UTF-8 of at most 255 bytes')
            request(path, 'rename', pane['id'], title.encode('utf-8'))
            print(f'kilix rename: renamed page {pane["tab"]}')
        else:
            closing = ([member['id'] for member in panes if member['tab'] == pane['tab']]
                       if args.target.startswith('tab:') else [pane['id']])
            for member in closing:
                request(path, 'close', member)
            print(f'kilix close: closed {"page" if len(closing)>1 or args.target.startswith("tab:") else "pane"} '
                  f'{pane["tab"] if args.target.startswith("tab:") else pane["id"]}')
        return
    if args.action in ('focus', 'watch'):
        pane = resolve(panes, args.target)
        if args.action == 'focus':
            request(path, 'focus', pane['id'])
            print(f'kilix focus: focused pane {pane["id"]}')
            return
        if not 0.05 <= args.interval <= 3600:
            parser.error('watch interval must be between 0.05 and 3600 seconds')
        try: own = self_pane(panes)['id']
        except ValueError: own = None
        if own == pane['id']:
            parser.error('Cannot watch the current pane from itself')
        while True:
            text = request(path, 'dump', pane['id'])
            if not args.once and sys.stdout.isatty():
                sys.stdout.write('\x1b[H\x1b[2J')
            sys.stdout.write(text)
            sys.stdout.flush()
            if args.once:
                return
            time.sleep(args.interval)
    source = resolve(panes, args.target) if args.target else self_pane(panes)
    directory = args.cwd
    if directory == 'current':
        directory = current_directory(source) if args.target else os.getcwd()
    directory = str(Path(directory).expanduser().resolve(strict=True))
    if not os.path.isdir(directory):
        parser.error('--cwd must name a directory')
    shell = managed_shell()
    for title in (args.tab_title, args.pane_title):
        if title is not None and (len(title.encode('utf-8')) > 255 or any(
                ord(char) < 32 or 127 <= ord(char) <= 159 or 0xd800 <= ord(char) <= 0xdfff
                for char in title)):
            parser.error('Titles must be printable UTF-8 of at most 255 bytes')
    for item in args.env:
        name, separator, value = item.partition('=')
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) or '\0' in value:
            parser.error('--env requires NAME=VALUE with a valid variable name')
    if not command:
        command = [os.environ.get('BATTY_SHELL', shell)]
    if args.env:
        command = ['/usr/bin/env', *args.env, *command]
    # Static script with positional arguments preserves arbitrary literal argv.
    command = [shell, '--noprofile', '--norc', '-c', 'cd -- "$1" || exit; shift; exec -- "$@"',
               'batty-launch', directory, *command]
    if any('\0' in arg for arg in command):
        parser.error('Command arguments cannot contain NUL bytes')
    if args.action == 'launch' and args.type == 'os-window':
        frontend = [str(ROOT / 'kilix')]
        if args.tab_title is not None:
            frontend += ['--window-title', args.tab_title, '--initial-title', args.tab_title]
        if args.pane_title is not None:
            frontend += ['--initial-pane-title', args.pane_title]
        environment = os.environ.copy()
        environment['BATTY_KILIX_SKIP_INITIAL_RECOVERY'] = '1'
        environment.pop('BATTY_CONTROL', None)
        child = subprocess.Popen([*frontend, '--', *command], cwd=directory, env=environment,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        print(f'kilix launch: opened window {child.pid}')
        return
    split = args.action in ('new-pane', 'split')
    direction = DIRECTIONS.index(args.direction) if split else 1
    name = 'kilix-auto-' + secrets.token_hex(12)
    payload = bytes((direction, 0)) + name.encode() + b'\0' + b'\0'.join(os.fsencode(arg) for arg in command) + b'\0'
    created = request(path, 'session', source['id'] if split else 0, payload)
    if args.tab_title is not None:
        request(path, 'rename', created['id'], args.tab_title.encode('utf-8'))
    if args.pane_title is not None:
        request(path, 'pane-rename', created['id'], args.pane_title.encode('utf-8'))
    if args.keep_focus and source:
        request(path, 'focus', source['id'])
    print(f'kilix {args.action}: opened {created["id"]}')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, ValueError, RuntimeError) as error:
        print(f'kilix: {error}', file=sys.stderr)
        sys.exit(1)
