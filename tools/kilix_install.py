#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""List and install the pinned catalog content supported by Batty."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

import kilix_content_app as app
import kilix_games

ROOT = Path(__file__).resolve().parent.parent


def application_ready(spec, installer):
    if spec.source_type in ('git', 'archive'):
        return bool(installer.ready(spec))
    if spec.content_id == 'dosbox':
        games, _ = kilix_games.backend()
        return bool(games.game_ready('dosbox'))
    if spec.content_id == 'kilix-camera-wall':
        from kilix_nvr_build import ready as patched_ready
        nvr = app.application_spec('kilix-nvr')
        selected = replace(nvr, build=(sys.executable, '-B',
                                       str(ROOT / 'tools/kilix_nvr_build.py')))
        executable = installer.ready(selected)
        return bool(executable and patched_ready(Path(executable).parent.parent))
    adapters = {
        'kilix-tmux-manager': (app.TMUX_TUI_REPOSITORY, app.TMUX_TUI_REF,
                               'tmux_tui.py', ()),
        'kilix-region-painter': (app.MASK_REPOSITORY, app.MASK_REF,
                                 'build/kilix-mask', ('make', 'all')),
        'kilix-model-store': (app.BONSAI_REPOSITORY, app.BONSAI_REF,
                              'build/kilix-bonsai',
                              (sys.executable, '-B', str(ROOT / 'tools/kilix_bonsai_build.py'))),
        'kilix-chawan': (app.CHAWAN_REPOSITORY, app.CHAWAN_REF,
                         'target/release/bin/cha',
                         (sys.executable, '-B', str(ROOT / 'tools/kilix_chawan_build.py'))),
    }
    if spec.content_id not in adapters:
        return False
    repository, ref, binary, build = adapters[spec.content_id]
    selected = replace(spec, source_type='git', repository=repository, ref=ref,
                       binary=binary, command=(), build=build)
    return bool(installer.ready(selected))


def rows():
    catalog = app.default_catalog()
    installer = app.Installer(app.apps_root())
    games, _ = kilix_games.backend()
    result = []
    for spec in catalog:
        if spec.kind not in ('app', 'game'):
            continue
        try:
            installed = (application_ready(spec, installer) if spec.kind == 'app'
                         else bool(games.game_ready(spec.content_id)))
        except (OSError, ValueError, app.InstallError):
            installed = False
        result.append({'id': spec.content_id, 'label': spec.label,
                       'kind': spec.kind, 'description': spec.description,
                       'installed': installed})
    return result


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args in ([], ['--list'], ['--json']):
        listed = rows()
        if args == ['--json']:
            print(json.dumps(listed, ensure_ascii=True))
        else:
            for row in listed:
                state = 'installed' if row['installed'] else 'available'
                print(f"{row['id']}\t{row['kind']}\t{state}\t{row['label']}")
        return 0
    if len(args) != 1 or args[0].startswith('-'):
        raise ValueError('usage: kilix install [--json|--list|ID]')
    spec = app.default_catalog().get(args[0])
    if spec is None or spec.kind not in ('app', 'game'):
        raise ValueError(f'unknown installable content: {args[0]}')
    command = ([str(ROOT / 'kilix'), 'app', 'install', spec.content_id]
               if spec.kind == 'app' else
               [str(ROOT / 'kilix'), 'games', 'install', spec.content_id])
    os.execv(command[0], command)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, app.CatalogError, app.InstallError) as error:
        print(f'kilix install: {error}', file=sys.stderr)
        raise SystemExit(1)
