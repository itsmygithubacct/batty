#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Keep private Kilix layouts and output across frontend, owner and reboot loss."""
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
import hashlib

from control import events, request
from control_paths import session_root
from kilix_workspace import restore_records, save, closed_owner
from kilix_recovery import capture, read, recovery_directory, private_directory, directory_lock

NAME = re.compile(r'kilix-auto-[0-9a-f]{24}\Z')
SNAPSHOT = re.compile(r'\.kilix-layout-[0-9a-f]{24}\.json\Z')


def remove_snapshot(path):
    """Remove only our manifest and its dedicated, private output directory."""
    with directory_lock(path.parent) as directory:
        path.unlink(missing_ok=True)
        output = path.parent / ('.batty-output-' + hashlib.sha256(path.name.encode()).hexdigest()[:24])
        try:
            private_directory(output)
            for archive in output.iterdir():
                if re.fullmatch(r'[0-9a-f]{64}\.bt-output', archive.name):
                    archive.unlink()
            output.rmdir()
        except (OSError, ValueError):
            pass
        os.fsync(directory)


def private_root(root):
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or
            info.st_mode & 0o077):
        raise ValueError('Automatic layout directory must be private and user-owned')


def stable(checkpoint):
    """Record only state needed to reconstruct the layout and owner mapping."""
    return {'version': checkpoint['version'], 'layout_format': checkpoint['layout_format'],
            'layout_hex': checkpoint['layout_hex'], 'appearance': checkpoint['appearance'],
            'panes': [{key: pane[key] for key in
                       ('id', 'session', 'session_dir', 'session_epoch', 'observe', 'persistent')}
                      for pane in checkpoint['panes']]}


def run(endpoint, root, parent, recovered=None):
    private_root(root)
    durable = recovery_directory(create=True)
    path = durable / ('.kilix-layout-' + secrets.token_hex(12) + '.json')
    previous = None
    archives = {}
    last_capture = 0
    cursor, epoch = 0, '0'
    while os.getppid() == parent:
        try:
            checkpoint = stable(request(endpoint, 'checkpoint', timeout=3))
            if not checkpoint['panes'] or any(
                    not isinstance(pane['session'], str) or
                    not NAME.fullmatch(pane['session']) or
                    not isinstance(pane['session_dir'], str) or Path(pane['session_dir']) != root
                    for pane in checkpoint['panes']):
                remove_snapshot(path)
                return  # Explicitly named sessions retain manual attach/restore policy.
            if checkpoint != previous or time.monotonic() - last_capture >= 5:
                current = {}
                for pane in checkpoint['panes']:
                    owner_epoch = pane['session_epoch']
                    if owner_epoch in current:
                        continue
                    try:
                        current[owner_epoch] = capture(pane)
                    except (OSError, RuntimeError, ValueError):
                        if owner_epoch not in archives:
                            raise
                        current[owner_epoch] = archives[owner_epoch]
                    if sum(map(len, current.values())) > 256 * 1024 * 1024:
                        raise ValueError('Automatic output exceeds the 256 MiB limit')
                save(path, checkpoint, current)
                archives = current
                previous = checkpoint
                last_capture = time.monotonic()
                if recovered:
                    old = Path(recovered)
                    if SNAPSHOT.fullmatch(old.name) and old.parent in (durable, root):
                        remove_snapshot(old)
                    recovered = None
            update = events(endpoint, cursor, epoch, timeout=3)
            cursor, epoch = update['cursor'], update['epoch']
        except (OSError, RuntimeError, ValueError):
            break


def select_snapshot(root, listing):
    try:
        durable = recovery_directory()
    except (OSError, ValueError):
        durable = None
    available, orphaned = set(), set()
    for line in listing.splitlines():
        words = line.split()
        # A dead owner's socket can remain in the runtime directory. Its
        # unavailable listing must not block selection of durable output;
        # the restore path independently checks whether replacement is safe.
        if words and NAME.fullmatch(words[0]) and 'unavailable' not in words:
            available.add(words[0])
            if 'controllers=0' in words:
                orphaned.add(words[0])
    candidates = []
    paths = []
    for directory in dict.fromkeys([root, durable]):
        if directory is None:
            continue
        try:
            private_root(directory)
            paths.extend(directory.iterdir())
        except (OSError, ValueError):
            continue
    for path in paths:
        if not SNAPSHOT.fullmatch(path.name):
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            continue
        candidates.append((info.st_mtime_ns, path))
        if len(candidates) > 512:
            return None
    selected = None
    for _, path in sorted(candidates, reverse=True):
        try:
            checkpoint = restore_records(path, return_document=True)
            panes = [pane for pane in checkpoint['panes']
                     if not closed_owner(pane['session_epoch'], durable)]
            if not panes:
                remove_snapshot(path)
                continue
            names = [pane['session'] for pane in panes]
            roots = [pane['session_dir'] for pane in panes]
            if not all(NAME.fullmatch(name) for name in names) or not all(
                    Path(owner_root) == root for owner_root in roots):
                continue
            if any(name not in available for name in names):
                for pane in panes:
                    output = pane.get('recovery_output')
                    if not output:
                        raise ValueError('Lost owner has no durable output')
                    archive = path.parent / output
                    private_directory(archive.parent)
                    if read(archive, archive.stem)['epoch'] != pane['session_epoch']:
                        raise ValueError('Saved output epoch changed')
            if selected is None and all(name not in available or name in orphaned for name in names):
                selected = path
        except (OSError, ValueError):
            continue
    return selected


def main():
    if len(sys.argv) in (4, 5) and sys.argv[1] == '_run':
        try:
            run(sys.argv[2], session_root(), int(sys.argv[3]), sys.argv[4] if len(sys.argv) == 5 else None)
        except (OSError, ValueError):
            pass  # A failed snapshot never prevents the terminal from running.
        return 0
    if sys.argv[1:] == ['_select']:
        try:
            path = select_snapshot(session_root(), sys.stdin.read(262144))
        except OSError:
            path = None
        if path is not None:
            print(path)
        return 0
    raise SystemExit('Usage: kilix_auto_workspace.py _run ENDPOINT PARENT | _select')


if __name__ == '__main__':
    raise SystemExit(main())
