#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Report Batty Kilix runtime and attached frontend status."""
import json
import os
from pathlib import Path
import re
import sys

from control import request
from control_paths import resolve_endpoint
from kilix_default_desktop import current

ROOT = Path(__file__).resolve().parent.parent


def status():
    record = ROOT / 'build/bash-os.json'
    try:
        runtime = json.loads(record.read_text())
    except (OSError, ValueError):
        runtime = {}
    revision = runtime.get('revision')
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40,64}', revision):
        revision = None
    result = {'host': 'Batty Kilix', 'bash_os_revision': revision,
              'default_desktop': current(), 'frontend': None}
    if os.environ.get('BATTY_CONTROL') or os.environ.get('BATTY_CONTROL_DIR'):
        try:
            panes = request(resolve_endpoint(), 'list')['panes']
        except (OSError, RuntimeError, ValueError, KeyError):
            pass
        else:
            result['frontend'] = {'panes': len(panes),
                                  'pages': len({pane['tab'] for pane in panes}),
                                  'running': sum(pane.get('exit_status') is None for pane in panes)}
    return result


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in ([], ['--json']):
        raise ValueError('usage: kilix status [--json]')
    result = status()
    if args:
        print(json.dumps(result, ensure_ascii=True))
        return 0
    print(result['host'])
    print('bash-os:', result['bash_os_revision'][:12] if result['bash_os_revision'] else 'unavailable')
    print('default desktop:', result['default_desktop'])
    frontend = result['frontend']
    if frontend:
        print(f"frontend: {frontend['pages']} pages, {frontend['panes']} panes, "
              f"{frontend['running']} running")
    else:
        print('frontend: not attached')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f'kilix status: {error}', file=sys.stderr)
        raise SystemExit(1)
