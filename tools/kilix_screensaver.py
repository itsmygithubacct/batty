#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build and run the pinned Kilix terminal screensaver from private storage."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

from kilix_apps import private_directory

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'third_party/kilix-desktop/src/config/screensavers/matrix.c'
DIGEST = '69db59c93c09f63046708ab98748615fdfe927fe10470f4bac493020de4a02b8'


def executable():
    if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != DIGEST:
        raise ValueError('Bundled screensaver source differs from its pinned revision')
    data = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    storage = Path(private_directory(os.environ.get('BATTY_KILIX_STORAGE_HOME') or data / 'batty/kilix'))
    cache = Path(private_directory(storage / 'cache/screensavers'))
    target = cache / ('matrix-' + DIGEST[:16])
    try:
        info = target.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_mode & 0o100:
            return target
    except FileNotFoundError:
        pass
    compiler = shutil.which('cc')
    if not compiler:
        raise ValueError('A C compiler is required to build the matrix screensaver')
    descriptor, temporary = tempfile.mkstemp(prefix='.matrix-', dir=cache)
    os.close(descriptor)
    try:
        subprocess.run([compiler, '-O2', '-o', temporary, str(SOURCE)],
                       check=True, timeout=60, capture_output=True)
        os.chmod(temporary, 0o700)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, prog='kilix screensaver')
    parser.add_argument('--install-only', action='store_true')
    parser.add_argument('name', nargs='?', default='matrix', choices=('matrix',))
    args = parser.parse_args(argv)
    target = executable()
    if args.install_only:
        print(target)
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error('the screensaver needs a terminal')
    os.execv(target, [str(target)])


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as error:
        print(f'kilix screensaver: {error}', file=sys.stderr)
        raise SystemExit(1)
