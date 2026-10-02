#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the pinned text desktop against Batty's host commands and facts."""
import importlib.util
from pathlib import Path
import subprocess
import sys

from kilix_apps import inside_batty
from kilix_status import status

ROOT = Path(__file__).resolve().parent.parent
HIDDEN = {
    'Machine': {'VirtualBox VPN'},
    'System': {'OS control', 'Update the stack'},
}
DESKTOP_CHOICES = {'auto', 'external', 'builtin', 'xp', 'tui', 'cap', 'land', 'icewm', 'none'}


def batty_facts():
    report = status()
    rows = [('host', report['host']),
            ('bash-os', (report['bash_os_revision'] or 'unavailable')[:12]),
            ('default', report['default_desktop']), ('provider', 'tui')]
    if frontend := report['frontend']:
        rows.append(('frontend', f"{frontend['pages']} pages · {frontend['panes']} panes"))
    return rows


def adapt(checkout):
    from kilix_desk import desk, facts, registry, sources
    from kilix_tui import kitty_rc

    sources.candidates = lambda: (str(ROOT),)
    facts.status_rows = batty_facts
    registry.SECTIONS = {
        name: tuple(item for item in items if item.label not in HIDDEN.get(name, ()))
        for name, items in registry.SECTIONS.items()
    }
    desk.State.DESKTOP_CHOICES = tuple(
        choice for choice in desk.State.DESKTOP_CHOICES if choice[0] in DESKTOP_CHOICES)
    desk.SECTIONS = tuple(name for name in desk.SECTIONS if name != 'Power')
    original_summary = desk._place_summary
    desk._place_summary = lambda state: (
        f'Everything Batty Kilix can do, in {len(desk.SECTIONS)} places'
        if not state.path else original_summary(state))

    kitty_rc.available = inside_batty

    def launch_tab(argv, *, title, cwd=None, keep_focus=True):
        command = [str(ROOT / 'kilix'), 'launch', '--type=tab', '--self',
                   '--tab-title', title]
        if cwd:
            command += ['--cwd', cwd]
        if keep_focus:
            command.append('--keep-focus')
        command += ['--', *argv]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as error:
            raise kitty_rc.Unavailable(str(error)) from error
        if result.returncode:
            raise kitty_rc.Unavailable(result.stderr.strip() or 'Batty page launch failed')
        try:
            return int(result.stdout.rsplit(' ', 1)[1])
        except (IndexError, ValueError):
            return 0

    kitty_rc.launch_tab = launch_tab

    def resolve(item):
        if item.label in ('Music', 'Cameras'):
            app_id = {'Music': 'kilix-music-control',
                      'Cameras': 'kilix-camera-manager'}[item.label]
            return registry.Plan((str(ROOT / 'kilix'), 'app', 'run', app_id), item.verb)
        if item.command in ('kilix-tts', 'kilix-stt'):
            return registry.Plan((str(ROOT / 'kilix'), item.command.removeprefix('kilix-')),
                                 item.verb)
        if item.label == 'Pane center':
            return registry.Plan((str(ROOT / 'kilix'), 'panes'), item.verb)
        if item.label == 'Tmux manager':
            return registry.Plan((str(ROOT / 'kilix'), 'tmux'), item.verb)
        if item.kilix:
            return registry.Plan((str(ROOT / 'kilix'), *item.kilix), item.verb)
        if item.sibling or item.source:
            relative = (Path('tools') / item.sibling / 'main.py' if item.sibling
                        else Path(item.source) / 'main.py')
            target = (checkout / relative).resolve()
            if target.is_relative_to(checkout) and target.is_file():
                return registry.Plan((sys.executable, '-B',
                                      str(ROOT / 'tools/kilix_tui_tool.py'),
                                      str(checkout), str(relative)), item.verb)
            return None
        if item.command == 'nmtui':
            from shutil import which
            if command := which('nmtui'):
                return registry.Plan((command,), item.verb)
        return None

    registry.resolve = resolve


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        raise ValueError('missing verified TUI checkout')
    checkout = Path(args.pop(0)).resolve(strict=True)
    entry = checkout / 'kilix-tui/main.py'
    if not entry.is_file() or not (checkout / 'src/kilix_desk/registry.py').is_file():
        raise ValueError('verified TUI checkout is incomplete')
    sys.path.insert(0, str(checkout / 'src'))
    adapt(checkout)
    spec = importlib.util.spec_from_file_location('batty_pinned_kilix_tui', entry)
    if spec is None or spec.loader is None:
        raise ValueError('cannot load pinned TUI entry point')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main(args)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f'kilix tui: {error}', file=sys.stderr)
        raise SystemExit(1)
