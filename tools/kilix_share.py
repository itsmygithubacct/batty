#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Share a fresh Batty Kilix desktop through the bundled X11 browser provider."""
import argparse
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SHARE_SOCKET_SUFFIX = '/batty-share-XXXXXXXX/sessions/kilix-auto-' + 'x' * 24 + '.sock'


def runtime_parent():
    preferred = os.environ.get('KILIX_SESSION_HOME') or os.environ.get('XDG_RUNTIME_DIR')
    for candidate in (preferred, tempfile.gettempdir(), '/tmp'):
        if candidate is None:
            continue
        path = Path(candidate).absolute()
        if len(os.fsencode(str(path))) + len(SHARE_SOCKET_SUFFIX) < 108:
            return path
    raise ValueError('No runtime path is short enough for shared desktop sockets')


def session(width, height):
    runtime = runtime_parent()
    with tempfile.TemporaryDirectory(prefix='batty-share-', dir=runtime) as directory:
        root = Path(directory)
        sessions, control = root / 'sessions', root / 'control'
        sessions.mkdir(mode=0o700)
        control.mkdir(mode=0o700)
        environment = os.environ.copy()
        environment.update(BATTY_SESSION_DIR=str(sessions), BATTY_CONTROL_DIR=str(control),
                           BATTY_KILIX_AUTO_RECOVER='0', BATTY_KILIX_SKIP_INITIAL_RECOVERY='1',
                           SDL_VIDEODRIVER='x11')
        environment.pop('WAYLAND_DISPLAY', None)
        environment.pop('BATTY_CONTROL', None)
        child = subprocess.Popen([str(ROOT / 'kilix'), '--width', str(width), '--height', str(height),
                                  '--initial-title', 'Shared Kilix'], env=environment)
        stopping = False
        def stop(_number, _frame):
            nonlocal stopping
            stopping = True
            if child.poll() is None:
                try:
                    child.terminate()
                except ProcessLookupError:
                    pass
        previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
        for number in previous:
            signal.signal(number, stop)
        try:
            while True:
                try:
                    return child.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    if stopping:
                        child.kill()
                        return child.wait()
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
            if child.poll() is None:
                child.kill()
                child.wait()
            environment['BATTY_OFFLINE'] = '1'
            for socket in sessions.glob('*.sock'):
                if socket.is_symlink() or not socket.is_socket():
                    continue
                try:
                    subprocess.run([str(ROOT / 'batty'), '--terminate', socket.stem,
                                    '--session-dir', str(sessions)], env=environment,
                                   capture_output=True, timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ['_session']:
        if len(argv) != 3:
            raise ValueError('Internal share session requires width and height')
        return session(int(argv[1]), int(argv[2]))
    parser = argparse.ArgumentParser(description=__doc__, prog='kilix share')
    parser.add_argument('--size', default='1280x800', help='Shared desktop pixels, WIDTHxHEIGHT')
    parser.add_argument('--fps', type=int, default=15)
    parser.add_argument('--lan', action='store_true', help='Serve HTTPS with a token on the LAN')
    parser.add_argument('--hls', action='store_true', help='Accepted for Kilix compatibility; always on')
    parser.add_argument('--audio', action='store_true', help='Include desktop audio in HLS')
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[1-9][0-9]*x[1-9][0-9]*', args.size):
        parser.error('--size must be WIDTHxHEIGHT')
    width, height = map(int, args.size.split('x'))
    if not 320 <= width <= 8192 or not 200 <= height <= 8192 or width % 2 or height % 2:
        parser.error('shared dimensions must be even, from 320x200 to 8192x8192')
    if not 1 <= args.fps <= 240:
        parser.error('--fps must be between 1 and 240')
    command = [str(ROOT / 'kilix'), 'run', '--serve', '--hls', '--no-pane', '--desktop-session',
               '--size', args.size, '--fps', str(args.fps)]
    if args.lan:
        command.append('--lan')
    if args.audio:
        command.append('--audio')
    if args.debug:
        command.append('--debug')
    command += ['--', sys.executable, '-B', str(Path(__file__).resolve()),
                '_session', str(width), str(height)]
    os.execv(command[0], command)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f'kilix share: {error}', file=sys.stderr)
        raise SystemExit(1)
