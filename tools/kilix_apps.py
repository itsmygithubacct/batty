#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Launch the bundled Kilix X11 application provider in a Batty pane."""
import argparse
import importlib.util
import os
from pathlib import Path
import re
import runpy
import shutil
import stat
import sys

ROOT = Path(__file__).resolve().parent.parent
PROVIDER = ROOT / 'third_party/kilix-apps/config/apprun.py'


def private_directory(path):
    path = Path(path).expanduser().absolute()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError(f'Application storage must be a private, user-owned directory: {path}')
    return str(path)


def provider_environment():
    data = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
    storage = private_directory(os.environ.get('BATTY_KILIX_STORAGE_HOME', data / 'batty/kilix'))
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    session = private_directory(Path(runtime) / 'batty-apps' if runtime else Path(storage) / 'session')
    os.environ.update(KILIX_STORAGE_HOME=storage, KILIX_SESSION_HOME=session,
                      KILIX_DATA_HOME=str(Path(storage) / 'data'),
                      KILIX_CONFIG_HOME=str(Path(storage) / 'config'),
                      KILIX_CACHE_HOME=str(Path(storage) / 'cache'),
                      KILIX_STATE_DIRECTORY=str(Path(storage) / 'state'),
                      KITTY_KILIX_RENDERING='1')
    # Other terminal hosts' session identities must not select frame taps or
    # reuse their provider runtime directories.
    for name in ('KILIX_SESSION', 'KILIX_NO_PANE', 'KITTY_WINDOW_ID', 'KITTY_PTY_BROKER_SESSION', 'KITTY_LISTEN_ON'):
        os.environ.pop(name, None)


def inside_batty():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    if not (os.environ.get('BATTY_CONTROL') or os.environ.get('BATTY_CONTROL_DIR')):
        return False
    from control_paths import resolve_endpoint
    from control import request
    from kilix_remote import self_pane
    try:
        self_pane(request(resolve_endpoint(), 'list')['panes'])
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__, prog='kilix run')
    parser.add_argument('--_in-pane', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--size', help='Fixed application pixels, WIDTHxHEIGHT; default follows pane size')
    parser.add_argument('--fps', type=int, default=20)
    for flag in ('serve', 'lan', 'hls', 'audio', 'mse', 'webrtc', 'no-pane', 'fill',
                 'refit-windows', 'no-refit-windows', 'desktop-session', 'debug'):
        options = ['--' + flag, '--ts'] if flag == 'mse' else ['--' + flag]
        parser.add_argument(*options, action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER, help='Application executable and arguments')
    args = parser.parse_args(argv)
    if args.command[:1] == ['--']:
        args.command = args.command[1:]
    if not args.command:
        parser.error('supply an application command after --')
    if args.size and not re.fullmatch(r'[1-9][0-9]*[xX][1-9][0-9]*', args.size):
        parser.error('--size must be positive WIDTHxHEIGHT pixels')
    if not 1 <= args.fps <= 240:
        parser.error('--fps must be between 1 and 240')
    if args.refit_windows and args.no_refit_windows:
        parser.error('choose either --refit-windows or --no-refit-windows')
    return args


def main(argv=None):
    original = sys.argv[1:] if argv is None else argv
    args = arguments(original)
    missing = [name for name in ('Xvfb', 'xauth', 'ffmpeg') if not shutil.which(name)]
    missing += [name for module, name in (('Xlib', 'python3-xlib'), ('PIL', 'python3-pil'))
                if importlib.util.find_spec(module) is None]
    if missing:
        raise RuntimeError('Missing application-provider dependencies: ' + ', '.join(missing))
    if not shutil.which(args.command[0]):
        raise ValueError(f'Application executable is unavailable: {args.command[0]}')
    if not args.no_pane and not inside_batty():
        # The child rechecks ancestry after the managed frontend launches it.
        if args._in_pane:
            raise RuntimeError('The new frontend could not identify its application pane')
        os.execv(str(ROOT / 'kilix'), [str(ROOT / 'kilix'), '--', sys.executable,
                                      '-B', str(Path(__file__).resolve()), '--_in-pane', *original])
    sys.dont_write_bytecode = True
    provider_environment()
    if args.lan or args.hls or args.mse or args.audio:
        from kilix_web_assets import prepare
        prepare(mse=args.mse)
    sys.path.insert(0, str(ROOT / 'third_party/kitty-frame-presenter/src'))
    sys.path.insert(0, str(PROVIDER.parent))
    forwarded = ['--fps', str(args.fps)]
    if args.size:
        forwarded += ['--size', args.size.lower()]
    for flag in ('serve', 'lan', 'hls', 'audio', 'mse', 'webrtc', 'no-pane', 'fill',
                 'refit-windows', 'no-refit-windows', 'desktop-session', 'debug'):
        if getattr(args, flag.replace('-', '_')):
            forwarded.append('--' + flag)
    sys.argv = [str(PROVIDER), *forwarded, *args.command]
    runpy.run_path(str(PROVIDER), run_name='__main__')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print(f'kilix run: {error}', file=sys.stderr)
        sys.exit(1)
