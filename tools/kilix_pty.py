#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Inspect and open Batty's persistent PTY owners."""
import argparse
import curses
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from control import request
from control_paths import frontend_endpoints, resolve_endpoint, session_root

ROOT = Path(__file__).resolve().parent.parent
NAME = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,47}\Z', re.ASCII)


def sessions(root):
    command = [str(ROOT / 'batty'), '--list', '--session-dir', str(root)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15,
                            env=os.environ | {'BATTY_OFFLINE': '1'})
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'Could not list Batty sessions')
    records = []
    for line in result.stdout.splitlines():
        name, *fields = line.split()
        if not NAME.fullmatch(name):
            raise ValueError('Invalid session name in native listing')
        if fields == ['unavailable']:
            records.append({'name': name, 'available': False})
            continue
        values = dict(field.split('=', 1) for field in fields)
        if set(values) != {'pid', 'cols', 'rows', 'status', 'exit', 'controllers', 'observers'}:
            raise ValueError('Invalid native session listing')
        if values['status'] not in ('running', 'exited'):
            raise ValueError('Invalid native session status')
        record = {'name': name, 'available': True, 'status': values['status']}
        for key in ('pid', 'cols', 'rows', 'exit', 'controllers', 'observers'):
            record[key] = int(values[key])
        records.append(record)
    return sorted(records, key=lambda item: item['name'])


def open_session(root, name, observe, socket=None):
    if not NAME.fullmatch(name):
        raise ValueError('Invalid Batty session name')
    try:
        endpoint = resolve_endpoint(socket)
    except ValueError:
        if socket or os.environ.get('BATTY_CONTROL') or os.environ.get('BATTY_CONTROL_DIR'):
            raise
        action = '--observe' if observe else '--attach'
        subprocess.Popen([str(ROOT / 'batty'), action, name, '--session-dir', str(root)],
                         env=os.environ | {'BATTY_OFFLINE': '1'},
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return 'Opened a separate Batty window'
    panes = request(endpoint, 'list')['panes']
    roots = {pane['session_dir'] for pane in panes if pane.get('session_dir')}
    if roots and str(root) not in roots:
        raise ValueError('This frontend uses a different Batty session root')
    if not observe:
        existing = next((pane for pane in panes if pane['session'] == name and
                         not pane['observe'] and pane['exit_status'] is None), None)
        if existing:
            request(endpoint, 'focus', existing['id'])
            return f'Focused pane {existing["id"]}'
        if os.environ.get('BATTY_CONTROL_DIR'):
            try:
                windows = frontend_endpoints()
            except FileNotFoundError:
                windows = []
            for window, other, other_panes in windows:
                if other == endpoint:
                    continue
                existing = next((pane for pane in other_panes if pane['session'] == name and
                                 pane['session_dir'] == str(root) and not pane['observe'] and
                                 pane['exit_status'] is None and not pane['disconnected']), None)
                if existing:
                    request(other, 'focus', existing['id'])
                    return f'Focused pane {existing["id"]} in {window}'
    payload = bytes((1, 2 if observe else 1)) + name.encode('ascii') + b'\0'
    created = request(endpoint, 'session', payload=payload)['id']
    title = ('View: ' if observe else 'Session: ') + name
    request(endpoint, 'rename', created, title.encode())
    return f'Opened pane {created}'


def terminate(root, name):
    if not NAME.fullmatch(name):
        raise ValueError('Invalid Batty session name')
    result = subprocess.run([str(ROOT / 'batty'), '--terminate', name,
                             '--session-dir', str(root)], capture_output=True,
                            text=True, timeout=15,
                            env=os.environ | {'BATTY_OFFLINE': '1'})
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f'Could not terminate {name}')


def interactive(root):
    def draw(stdscr):
        curses.curs_set(0)
        stdscr.timeout(1000)
        selected = 0
        message = ''
        while True:
            records = sessions(root)
            selected = min(selected, max(0, len(records) - 1))
            height, width = stdscr.getmaxyx()
            stdscr.erase()
            stdscr.addnstr(0, 0, 'Batty PTY sessions  ↑↓ select  a attach  v view  x terminate  q quit', width - 1)
            if not records:
                stdscr.addnstr(2, 0, 'No persistent sessions', width - 1)
            visible = max(0, height - 4)
            start = max(0, selected - visible + 1) if visible else 0
            for index, record in enumerate(records[start:start + visible], start):
                state = (f'{record["status"]}  {record["cols"]}×{record["rows"]}  '
                         f'{record["controllers"]} controller  {record["observers"]} observer'
                         if record['available'] else 'unavailable')
                line = f'{record["name"]:<50} {state}'
                stdscr.addnstr(index - start + 2, 0, line, width - 1,
                               curses.A_REVERSE if index == selected else 0)
            stdscr.addnstr(height - 1, 0, message, width - 1)
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord('q'), 27):
                return
            if key in (curses.KEY_UP, ord('k')):
                selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, ord('j')):
                selected = min(max(0, len(records) - 1), selected + 1)
            elif key in (ord('a'), ord('v'), ord('x')) and records:
                item = records[selected]
                if not item['available']:
                    message = 'Unavailable session cannot be opened'
                    continue
                try:
                    if key == ord('x'):
                        stdscr.addnstr(height - 1, 0,
                                       f'Terminate {item["name"]}? Press y to confirm', width - 1)
                        stdscr.clrtoeol()
                        stdscr.refresh()
                        stdscr.timeout(-1)
                        confirm = stdscr.getch()
                        stdscr.timeout(1000)
                        if confirm != ord('y'):
                            message = 'Termination cancelled'
                            continue
                        terminate(root, item['name'])
                        message = f'Terminated {item["name"]}'
                    else:
                        message = open_session(root, item['name'], key == ord('v'))
                except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                    message = str(error)
    curses.wrapper(draw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, prog='kilix pty')
    parser.add_argument('--session-dir', type=Path, default=session_root())
    parser.add_argument('--socket', help='Explicit Batty frontend endpoint')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--install-only', action='store_true', help='Built into Batty; no download needed')
    parser.add_argument('--yes', action='store_true', help='Confirm explicit termination')
    parser.add_argument('action', nargs='?', choices=('list', 'attach', 'view', 'terminate'))
    parser.add_argument('name', nargs='?')
    args = parser.parse_args(argv)
    root = args.session_dir.expanduser().absolute()
    if args.install_only:
        if args.action or args.name:
            parser.error('--install-only takes no action')
        print('Batty PTY manager is ready')
    elif args.action == 'list' or args.json:
        if args.name or args.action not in (None, 'list'):
            parser.error('list takes no session name')
        records = sessions(root)
        if args.json:
            print(json.dumps({'schema': 'batty.pty-sessions/v1', 'session_dir': str(root),
                              'sessions': records}))
        else:
            for record in records:
                print(record['name'], record.get('status', 'unavailable'),
                      f'{record.get("controllers", 0)} controller(s)')
    elif args.action in ('attach', 'view'):
        if not args.name:
            parser.error(f'{args.action} requires a session name')
        print(open_session(root, args.name, args.action == 'view', args.socket))
    elif args.action == 'terminate':
        if not args.name or not args.yes:
            parser.error('terminate requires NAME and --yes')
        terminate(root, args.name)
        print(f'Terminated {args.name}')
    elif args.name:
        parser.error('unexpected session name')
    elif not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error('interactive manager needs a terminal; use list or --json')
    else:
        interactive(root)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        print(f'kilix pty: {error}', file=sys.stderr)
        raise SystemExit(1)
