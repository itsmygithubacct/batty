#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Open Kilix laptop profiles as Batty windows, pages, and panes."""
import os
from pathlib import Path
import re
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import time

from control import request
from kilix_apps import private_directory

ROOT = Path(__file__).resolve().parent.parent
USAGE = ('usage: kilix laptop [list|status|open PROFILE|close PROFILE]\n'
         '  profiles use KEY=value files in the Batty-private laptop directory; '
         'KILIX_LAPTOP_PROFILES overrides it')
IDENTIFIER = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9._-]{0,38}\Z')
PANE_KEY = re.compile(r'pane\.([ \t]*[+-]?[0-9]+)\.(title|cwd|ssh|cmd)\Z')
DESTINATION = re.compile(r'[A-Za-z0-9._@-]+\Z')


class ProfileError(ValueError):
    pass


def profiles_directory():
    override = os.environ.get('KILIX_LAPTOP_PROFILES')
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ProfileError('KILIX_LAPTOP_PROFILES must be absolute')
    else:
        data = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
        storage = Path(os.environ.get('BATTY_KILIX_STORAGE_HOME') or data / 'batty/kilix')
        if not storage.is_absolute():
            raise ProfileError('Batty Kilix storage directory must be absolute')
        path = storage / 'laptop'
    return Path(private_directory(path))


def valid_id(value):
    return bool(IDENTIFIER.fullmatch(value)) and not value.startswith('.')


def profile_path(profile_id):
    if not valid_id(profile_id):
        raise ProfileError('invalid laptop profile ID')
    return profiles_directory() / (profile_id + '.profile')


def load_profile(profile_id):
    path = profile_path(profile_id)
    try:
        with path.open('rb') as stream:
            data = stream.read(16385)
    except OSError as error:
        raise ProfileError(f'cannot read profile {profile_id}') from error
    if len(data) >= 16384:
        raise ProfileError('profile exceeds 16 KiB')
    profile = {'id': profile_id, 'name': profile_id, 'desktop': '',
               'layout': 'splits', 'panes': {}}
    for raw in data.decode('latin-1').split('\n'):
        line = (raw[:-1] if raw.endswith('\r') else raw).lstrip(' \t')
        if not line or line.startswith('#'):
            continue
        key, equal, value = line.partition('=')
        if not equal or any(ord(char) < 32 or ord(char) == 127 or char == '"' for char in value):
            raise ProfileError('profile values must be plain KEY=value text')
        if key == 'name':
            if not value or len(value) >= 48:
                raise ProfileError('profile name must be 1..47 bytes')
            profile['name'] = value
        elif key == 'desktop':
            if value not in ('desktop', '95', 'xp', 'cap', 'tui', 'land', 'icewm'):
                raise ProfileError('unknown desktop provider')
            profile['desktop'] = value
        elif key == 'layout':
            if value not in ('splits', 'tabs'):
                raise ProfileError('layout must be splits or tabs')
            profile['layout'] = value
        elif match := PANE_KEY.fullmatch(key):
            number = int(match[1])
            field = match[2]
            if not 1 <= number <= 8:
                raise ProfileError('pane numbers must be 1..8')
            if len(value) >= (48 if field == 'title' else 200):
                raise ProfileError(f'pane {number} {field} is too long')
            if field == 'ssh' and (not DESTINATION.fullmatch(value) or value.startswith('-')):
                raise ProfileError('ssh destination must be [user@]host')
            profile['panes'].setdefault(number, {})[field] = value
        else:
            raise ProfileError(f'unknown profile key {key!r}')
    panes = profile['panes']
    if profile['desktop']:
        if panes:
            raise ProfileError('a profile cannot specify desktop and panes')
    elif not panes or set(panes) != set(range(1, max(panes) + 1)):
        raise ProfileError('pane numbers must start at 1 and be contiguous')
    return profile


def scan_profiles():
    return sorted(path.stem for path in profiles_directory().glob('*.profile')
                  if valid_id(path.stem) and path.is_file())


def run_directory():
    return Path(private_directory(profiles_directory() / 'run'))


def record_path(profile_id):
    return run_directory() / (profile_id + '.pid')


def process_identity(pid):
    try:
        with open(f'/proc/{pid}/stat', 'rb') as stream:
            if os.fstat(stream.fileno()).st_uid != os.geteuid():
                raise ProfileError('session process belongs to another user')
            fields = stream.read().rsplit(b') ', 1)[1].split()
        if fields[0] in (b'Z', b'X'):
            return None
        start = str(int(fields[19]))
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if not re.fullmatch(r'[0-9a-f-]{36}', boot):
            raise ValueError('invalid boot ID')
        return boot, start
    except FileNotFoundError:
        return None
    except (OSError, ValueError, IndexError) as error:
        raise ProfileError('cannot verify session process identity') from error


def clear_record(profile_id):
    record_path(profile_id).unlink(missing_ok=True)


def session_pid(profile_id):
    path = record_path(profile_id)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'r', encoding='ascii') as stream:
            lines = stream.read(1024).splitlines()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as error:
        raise ProfileError('cannot read laptop run registry') from error
    try:
        pid = int(lines[0])
    except (IndexError, ValueError) as error:
        raise ProfileError('invalid laptop run registry') from error
    if pid <= 1:
        raise ProfileError('invalid laptop session PID')
    fields = dict(line.split('=', 1) for line in lines[1:] if '=' in line)
    current = process_identity(pid)
    if current is None or current != (fields.get('boot_id'), fields.get('start_time')):
        if current is not None and not all((fields.get('boot_id'), fields.get('start_time'))):
            raise ProfileError('unverified legacy session; close its window manually')
        clear_record(profile_id)
        return None
    return pid


def write_record(profile_id, pid):
    identity = process_identity(pid)
    if identity is None:
        raise ProfileError('the session exited immediately')
    target = record_path(profile_id)
    fd, temporary = tempfile.mkstemp(prefix='.' + profile_id + '.', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='ascii') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(f'{pid}\nboot_id={identity[0]}\nstart_time={identity[1]}\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def endpoint(profile_id):
    control_dir = run_directory() / (profile_id + '.control')
    for path in control_dir.glob('front-*/control.sock'):
        try:
            panes = request(str(path), 'list', timeout=.3)['panes']
            if panes:
                return str(path), panes
        except (OSError, ValueError, RuntimeError):
            pass
    return None


def pane_command(pane):
    command = pane.get('cmd', '')
    remote = pane.get('ssh', '')
    cwd = pane.get('cwd', '')
    if remote:
        script = ((f'cd {shlex.quote(cwd)} && ' if cwd else '')
                  + (f'exec {command}' if command else 'exec "$SHELL" -l'))
        return ['ssh', '-t', remote, script] if (cwd or command) else ['ssh', '-t', remote]
    return ['/bin/sh', '-lc', command] if command else []


def local_cwd(pane):
    return str(Path((pane.get('cwd') if not pane.get('ssh') else '') or Path.cwd())
               .expanduser().resolve(strict=True))


def child_environment(profile_id):
    environment = os.environ.copy()
    environment['BATTY_CONTROL_DIR'] = str(Path(private_directory(
        run_directory() / (profile_id + '.control'))))
    environment['BATTY_SESSION_DIR'] = str(Path(private_directory(
        run_directory() / (profile_id + '.sessions'))))
    environment['BATTY_KILIX_SKIP_INITIAL_RECOVERY'] = '1'
    environment.pop('BATTY_CONTROL', None)
    return environment


def open_profile(profile_id):
    profile = load_profile(profile_id)
    if profile['desktop']:
        if profile['desktop'] not in ('desktop', '95', 'xp', 'tui', 'cap', 'land', 'icewm'):
            raise ProfileError(f'{profile["desktop"]} desktop provider is not ported to Batty')
        command = [str(ROOT / 'kilix'), 'desktop']
        if profile['desktop'] in ('xp', 'tui', 'cap', 'land', 'icewm'):
            command.append(profile['desktop'])
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        time.sleep(.3)
        if child.poll() not in (None, 0):
            raise ProfileError('the desktop provider did not start')
        print(f'laptop {profile_id}: opened (desktop profile, not tracked)')
        return
    if (pid := session_pid(profile_id)) is not None:
        print(f'laptop {profile_id}: already running (pid {pid})')
        return
    if endpoint(profile_id):
        raise ProfileError('an untracked window for this profile is already open')
    first = profile['panes'][1]
    title = profile['name']
    first_page = first.get('title') or title if profile['layout'] == 'tabs' else title
    args = [str(ROOT / 'kilix'), '--window-title', title, '--initial-title', first_page,
            '--initial-pane-title', first.get('title') or title]
    command = pane_command(first)
    if command:
        args += ['--', *command]
    child = subprocess.Popen(args, cwd=local_cwd(first), env=child_environment(profile_id),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        write_record(profile_id, child.pid)
        deadline = time.monotonic() + 12
        live = None
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise ProfileError('the session exited before its window was ready')
            live = endpoint(profile_id)
            if live:
                break
            time.sleep(.05)
        if live is None:
            raise ProfileError('the session window did not become ready')
        socket, panes = live
        source = panes[0]['id']
        for number in range(2, max(profile['panes']) + 1):
            pane = profile['panes'][number]
            tab = profile['layout'] == 'tabs'
            action = 'new-page' if tab else 'new-pane'
            launch = [str(ROOT / 'kilix'), action]
            if not tab:
                launch.append('right' if number % 2 == 0 else 'down')
            launch += ['--socket', socket, '--target', str(source)]
            if tab:
                launch += ['--tab-title', pane.get('title') or title]
            launch += ['--pane-title', pane.get('title') or title,
                       '--cwd', local_cwd(pane)]
            if command := pane_command(pane):
                launch += ['--', *command]
            result = subprocess.run(launch, capture_output=True, text=True, timeout=15,
                                    env=child_environment(profile_id))
            if result.returncode:
                raise ProfileError(result.stderr.strip() or f'cannot open pane {number}')
            source = int(result.stdout.rsplit(' ', 1)[1])
        print(f'laptop {profile_id}: opened (pid {child.pid})')
    except BaseException:
        if live := endpoint(profile_id):
            socket, panes = live
            for pane in reversed(panes):
                try:
                    request(socket, 'close', pane['id'], timeout=2)
                except (OSError, ValueError, RuntimeError):
                    pass
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        clear_record(profile_id)
        raise


def close_profile(profile_id):
    profile_path(profile_id)
    if (pid := session_pid(profile_id)) is None:
        print(f'laptop {profile_id}: not running')
        return
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise ProfileError('safe close requires Linux process descriptors')
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        clear_record(profile_id)
        print(f'laptop {profile_id}: closed')
        return
    try:
        if session_pid(profile_id) != pid:
            raise ProfileError('session process changed before close')
        if live := endpoint(profile_id):
            socket, panes = live
            for pane in reversed(panes):
                try:
                    request(socket, 'close', pane['id'], timeout=2)
                except (OSError, ValueError, RuntimeError):
                    pass
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        except ProcessLookupError:
            pass
        poller = select.poll()
        poller.register(descriptor, select.POLLIN)
        if not poller.poll(5000):
            raise ProfileError(f'laptop {profile_id}: still shutting down (pid {pid})')
        clear_record(profile_id)
    finally:
        os.close(descriptor)
    print(f'laptop {profile_id}: closed')


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    action = args.pop(0) if args else 'status'
    if action in ('help', '-h', '--help'):
        print(USAGE)
        return 0
    if action == 'list' and not args:
        for profile_id in scan_profiles():
            print(profile_id)
        return 0
    if action == 'status' and not args:
        for profile_id in scan_profiles():
            try:
                profile = load_profile(profile_id)
                if profile['desktop']:
                    state = ('desktop' if profile['desktop'] in ('desktop', '95', 'xp', 'tui', 'cap', 'land', 'icewm')
                             else 'unsupported')
                else:
                    state = f'running (pid {pid})' if (pid := session_pid(profile_id)) else 'stopped'
            except ProfileError:
                state = 'invalid'
            print(profile_id, state)
        return 0
    if action in ('open', 'close') and len(args) == 1:
        (open_profile if action == 'open' else close_profile)(args[0])
        return 0
    raise ProfileError(USAGE)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ProfileError, subprocess.TimeoutExpired) as error:
        print(f'kilix laptop: {error}', file=sys.stderr)
        raise SystemExit(1)
