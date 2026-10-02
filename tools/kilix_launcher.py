#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Bind the pinned TUI launcher to Batty's application catalog."""
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent

PROGRAM_APPS = {
    'Coding agents': 'kilix-rollout-resume',
    'Model store': 'kilix-model-store',
    'Region painter': 'kilix-region-painter',
    'PDF Conversion': 'kilix-pdf-conversion',
    'Music': 'kilix-music-control',
    'Weather': 'kilix-weather',
    'Calculator': 'kilix-calculator',
    'File manager': 'kilix-file',
    'Find files': 'kilix-find-files',
    'Notepad': 'kilix-notepad',
    'Character map': 'kilix-character-map',
}
HOST_VERBS = {
    'Web browser': ('open-url',),
    'Text browser': ('app', 'run', 'kilix-chawan'),
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise ValueError('usage: kilix-launcher-adapter INSTALLED-LAUNCHER [ARG...]')
    installed = Path(argv.pop(0)).resolve(strict=True)
    checkout = installed.parent.parent.parent
    source = checkout / 'tools/launcher/main.py'
    if installed != checkout / '.runtime/bin/kilix-launcher' or not source.is_file():
        raise ValueError('expected the pinned kilix-tui-utils launcher')
    spec = importlib.util.spec_from_file_location('_batty_kilix_launcher', source)
    if spec is None or spec.loader is None:
        raise ValueError('cannot load the pinned launcher')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.registry.kilix_command = lambda: [str(ROOT / 'kilix')]
    programs = []
    for item in module.registry.PROGRAMS:
        if item.submenu:
            programs.append(item)
        elif item.label in PROGRAM_APPS:
            programs.append(replace(item, command=None, sibling=None, source=None,
                                    kilix=('app', 'run', PROGRAM_APPS[item.label])))
        elif item.label in HOST_VERBS:
            programs.append(replace(item, command=None, sibling=None, source=None,
                                    kilix=HOST_VERBS[item.label]))
    module.registry.PROGRAMS = tuple(programs)
    return module.main(argv)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f'kilix launcher: {error}', file=sys.stderr)
        raise SystemExit(1)
