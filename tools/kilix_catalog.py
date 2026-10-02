#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Discover and launch installed XDG applications through the Kilix port."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

from kilix_apps import inside_batty
from desktop_exec import expand_exec

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    'batty_xdgapps', ROOT / 'third_party/kilix-apps/config/kilix_sdk/xdgapps.py')
xdgapps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(xdgapps)


def entries():
    result = []
    for entry in xdgapps.scan():
        parsed = xdgapps.parse_desktop_file(entry['path'])
        fresh = xdgapps.build_entry(parsed, entry['path'], entry['id']) if parsed else None
        if fresh:
            result.append(fresh | {'category': xdgapps.bucket(fresh),
                                   'exec_raw': xdgapps.unescape(parsed['Exec'])})
    return result


def launch_command(entry, inside, in_place=False, inputs=()):
    command = expand_exec(entry['exec_raw'], entry, inputs)
    cwd = entry['workdir'] or os.getcwd()
    if not os.path.isabs(cwd) or not os.path.isdir(cwd):
        raise ValueError('Application working directory must be an existing absolute directory')
    launcher = str(ROOT / 'kilix')
    if not entry['terminal']:
        command = [launcher, 'run', '--', *command]
    if inside and not in_place:
        command = [launcher, 'new-page', '--cwd', cwd, '--', *command]
    elif entry['terminal'] and not in_place:
        command = [launcher, '--', *command]
    return command, cwd


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, prog='kilix apps')
    sub = parser.add_subparsers(dest='action', required=True)
    listing = sub.add_parser('list', help='List installed applications')
    listing.add_argument('--json', action='store_true')
    opening = sub.add_parser('open', help='Launch an application by its desktop ID')
    opening.add_argument('id')
    opening.add_argument('inputs', nargs='*', metavar='FILE_OR_URL')
    opening.add_argument('--in-place', action='store_true', help='Run in the current pane/process')
    sub.add_parser('_menu', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    catalog = entries()
    if args.action == '_menu':
        for entry in catalog[:256]:
            name = ''.join(c if c.isprintable() else ' ' for c in entry['name'])
            name = name.encode('utf-8')[:255].decode('utf-8', errors='ignore') or entry['id']
            sys.stdout.buffer.write(name.encode() + b'\0' + entry['id'].encode() + b'\0')
        sys.stdout.buffer.write(b'DONE\0')
        return
    if args.action == 'list':
        if args.json:
            print(json.dumps(catalog, ensure_ascii=True))
        else:
            for entry in catalog:
                print('\t'.join(''.join(c if c.isprintable() else ' ' for c in entry[key])
                                for key in ('id', 'category', 'name')))
        return
    entry = next((entry for entry in catalog if entry['id'] == args.id), None)
    if entry is None:
        raise ValueError('No installed application matches desktop ID: ' + args.id)
    command, cwd = launch_command(entry, inside_batty(), args.in_place, args.inputs)
    os.chdir(cwd)
    os.execvp(command[0], command)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print(f'kilix apps: {error}', file=sys.stderr)
        sys.exit(1)
