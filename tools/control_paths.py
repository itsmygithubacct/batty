#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Private frontend registry and persistent-child endpoint discovery."""
from concurrent.futures import ThreadPoolExecutor
import itertools
import os
from pathlib import Path
import re
import stat
import sys


def ancestors():
    pid, seen = os.getpid(), set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        yield pid
        try:
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            pid = int(fields[1])
        except (OSError, ValueError, IndexError):
            break


def session_root():
    """Match the native Bash builtin's default persistent-session root."""
    value = os.environ.get('BATTY_SESSION_DIR')
    if value:
        return Path(value)
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    return Path(runtime) / 'batty' if runtime else Path(f'/tmp/batty-{os.geteuid()}')


def private(path, directory):
    value = path.lstat()
    kind = stat.S_ISDIR(value.st_mode) if directory else stat.S_ISSOCK(value.st_mode)
    if not kind or value.st_uid != os.geteuid() or value.st_mode & 0o077:
        raise ValueError(f'Control registry path must be private and owned by this user: {path}')


def registry(create=False):
    configured = os.environ.get('BATTY_CONTROL_DIR')
    if configured:
        root = Path(configured)
    elif os.environ.get('XDG_RUNTIME_DIR'):
        root = Path(os.environ['XDG_RUNTIME_DIR']) / 'batty-control'
    else:
        root = Path(f'/tmp/batty-control-{os.geteuid()}')
    if not root.is_absolute():
        raise ValueError('Control registry must be an absolute path')
    if create:
        try: root.mkdir(mode=0o700)
        except FileExistsError: pass
    private(root, True)
    return root


def frontend_endpoints():
    """Return live private frontend endpoints with registry-local window IDs."""
    root = registry()
    with os.scandir(root) as entries:
        candidates = list(itertools.islice(entries, 129))
    if len(candidates) > 128:
        raise ValueError('Control registry exceeds its 128-entry discovery limit')

    def inspect(entry):
        from control import request
        try:
            if not entry.is_dir(follow_symlinks=False) or not re.fullmatch(r'front-[A-Za-z0-9]{8}', entry.name):
                return None
            path = Path(entry.path) / 'control.sock'
            private(path.parent, True)
            private(path, False)
            panes = request(str(path), 'list', timeout=0.3)['panes']
            return entry.name, str(path), panes
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        return sorted((item for item in pool.map(inspect, candidates) if item), key=lambda item: item[0])


def resolve_window(identity):
    if not re.fullmatch(r'front-[A-Za-z0-9]{8}', identity):
        raise ValueError('Window ID must come from kilix ls --all')
    matches = [path for name, path, _ in frontend_endpoints() if name == identity]
    if len(matches) != 1:
        raise ValueError(f'No live Kilix window matches {identity}')
    return matches[0]


def resolve_endpoint(explicit=None):
    """Explicit endpoints bypass discovery; defaults must follow the caller.

    Without a registry environment (custom hosts/older sessions), preserve the
    BATTY_CONTROL contract. With a registry, require a live non-observer view
    whose PTY process is an ancestor, never another pane merely sharing a host.
    """
    if explicit:
        return explicit
    preferred = os.environ.get('BATTY_CONTROL')
    if not os.environ.get('BATTY_CONTROL_DIR'):
        if not preferred:
            raise ValueError('Set BATTY_CONTROL or supply --socket PATH')
        return preferred
    root = registry()
    lineage = set(ancestors())

    def owns(path):
        from control import request
        try:
            private(path.parent, True)
            private(path, False)
            panes = request(str(path), 'list', timeout=0.3)['panes']
            return any(p['pid'] in lineage and p['exit_status'] is None and not p['observe'] for p in panes)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            return False

    if preferred and owns(Path(preferred)):
        return preferred
    with os.scandir(root) as entries:
        candidates = list(itertools.islice(entries, 129))
    if len(candidates) > 128:
        raise ValueError('Control registry exceeds its 128-entry discovery limit; supply --socket PATH')
    paths = [Path(entry.path) / 'control.sock' for entry in candidates
             if entry.name.startswith('front-') and entry.is_dir(follow_symlinks=False)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        matches = [str(path) for path, matched in zip(paths, pool.map(owns, paths)) if matched]
    if len(matches) != 1:
        raise ValueError('Cannot find one attached controlling pane for this process; supply --socket PATH')
    return matches[0]


if __name__ == '__main__':
    try:
        print(registry(create=True))
    except (OSError, ValueError) as error:
        print(f'Batty: {error}', file=sys.stderr)
        sys.exit(1)
