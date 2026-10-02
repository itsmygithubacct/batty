#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Open named Batty-owned terminal sessions in a Kilix frontend."""
import argparse
import os
import re

from control import request
from control_paths import frontend_endpoints, resolve_endpoint, session_root
from kilix_remote import managed_shell


def controller(panes, name, root=None):
    return next((pane for pane in panes if pane['session'] == name and
                 not pane['observe'] and pane['exit_status'] is None and
                 not pane['disconnected'] and
                 (root is None or pane['session_dir'] == root)), None)


def main():
    parser = argparse.ArgumentParser(description=__doc__, prog='kilix')
    parser.add_argument('action', choices=('serve', 'attach', 'view'))
    parser.add_argument('name', nargs='?', default='main')
    parser.add_argument('--socket', help='Explicit frontend endpoint')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,32}', args.name, flags=re.ASCII):
        parser.error('Session name must be 1–33 ASCII letters, digits, underscores or hyphens')
    name = 'kilix-mux-' + args.name
    mode = {'serve': 0, 'attach': 1, 'view': 2}[args.action]
    try:
        path = resolve_endpoint(args.socket)
        if mode != 2:
            panes = request(path, 'list')['panes']
            existing = controller(panes, name)
            if existing:
                request(path, 'focus', existing['id'])
                print(f'kilix {args.action}: opened pane {existing["id"]} in {name}')
                return
            roots = {pane['session_dir'] for pane in panes if pane['session_dir']}
            root = next(iter(roots)) if len(roots) == 1 else str(session_root()) if not roots else None
            if root and os.path.isabs(root) and os.environ.get('BATTY_CONTROL_DIR'):
                for window, endpoint, other_panes in frontend_endpoints():
                    if endpoint == path:
                        continue
                    existing = controller(other_panes, name, root)
                    if existing:
                        request(endpoint, 'focus', existing['id'])
                        print(f'kilix {args.action}: opened pane {existing["id"]} '
                              f'in {name} (window {window})')
                        return
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f'kilix {args.action}: {error}\n')
    command = []
    if mode == 0:
        shell = os.environ.get('BATTY_SHELL') or managed_shell()
        if not os.path.isabs(shell) or not os.access(shell, os.X_OK):
            parser.error('BATTY_SHELL must be an executable absolute path')
        command = [os.fsencode(shell)]
    payload = bytes((1, mode)) + name.encode('ascii') + b'\0'
    if command:
        payload += b'\0'.join(command) + b'\0'
    try:
        created = request(path, 'session', 0, payload)
        title = f'Mux: {args.name}' + (' (view)' if mode == 2 else '')
        request(path, 'rename', created['id'], title.encode('utf-8'))
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f'kilix {args.action}: {error}\n')
    print(f'kilix {args.action}: opened pane {created["id"]} in {name}')


if __name__ == '__main__':
    main()
