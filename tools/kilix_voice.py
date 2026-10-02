#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the pinned Kilix Voice tools from Batty-private source and state."""
import os
import json
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import time

import kilix_content_app as app
from kilix_apps import inside_batty, private_directory
from control import request
from control_paths import ancestors, resolve_endpoint

ROOT = Path(__file__).resolve().parent.parent
BUNDLED = ROOT / 'third_party/kilix-desktop/src'
VOICE_INSTALLER = BUNDLED / 'scripts/install-kilix-voice.sh'
REPOSITORY = 'https://github.com/itsmygithubacct/kilix-voice.git'
REF = 'f501409a82bf73b738b14986e12441bce23ec1c6'


def spec(tool):
    return app.ContentSpec(content_id='kilix-voice-' + tool,
                           package_id='kilix-voice', label='Kilix Voice ' + tool.upper(),
                           kind='app', icon='', description='Pinned local voice tool',
                           source_type='git', repository=REPOSITORY, ref=REF,
                           binary='kilix-' + tool)


def installer():
    return app.Installer(app.apps_root())


def environment():
    storage = Path(app.apps_root()).parent.parent
    session = private_directory(storage / 'session')
    private_directory(storage / 'data')
    private_directory(storage / 'config')
    selected = os.environ.copy()
    selected.update(GPU_TERMINAL_HOME=str(storage), KILIX_STORAGE_HOME=str(storage),
                    KILIX_DATA_HOME=str(storage / 'data'),
                    KILIX_CONFIG_HOME=str(storage / 'config'),
                    KILIX_SESSION_HOME=session,
                    KILIX_STATE_DIRECTORY=str(storage / 'state'),
                    KILIX_HOME=str(ROOT),
                    GPU_TERMINAL_SOURCE_HOME=str(storage / 'data/sources'),
                    KILIX_VOICE_PREFIX=str(storage / 'data/voice/prefix'),
                    PATH=str(ROOT) + os.pathsep + os.environ.get('PATH', ''))
    selected.pop('KILIX_RC_PASSWORD_FILE', None)
    return selected


def tool_command(tool, arguments):
    selected = spec(tool)
    executable = installed_tool(tool) or installer().ensure(
        selected, lambda message: print(f'kilix {tool}: {message}', file=sys.stderr))
    return [executable, *arguments]


def installed_tool(tool):
    selected = Path(environment()['KILIX_VOICE_PREFIX']) / 'bin' / ('kilix-' + tool)
    if selected.is_file() and os.access(selected, os.X_OK):
        return str(selected)
    return installer().ready(spec(tool))


def voice_request(operation, **fields):
    path = Path(environment()['KILIX_SESSION_HOME']) / 'voice/control.sock'
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('voice control socket is not owned by this user')
    request = json.dumps({'op': operation, **fields}, ensure_ascii=False).encode() + b'\n'
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
        client.settimeout(5)
        client.connect(str(path))
        client.sendall(request)
        reply = json.loads(client.recv(65536))
    if not reply.get('ok'):
        raise ValueError(str(reply.get('error', 'voice daemon rejected the request')))
    return reply


def pane_text(endpoint, pane):
    visible = request(endpoint, 'dump', pane['id']).strip()
    if not visible:
        raise ValueError('the active Batty pane has no visible text to read aloud')
    return visible[:4000]


def active_pane(endpoint=None, target=None):
    endpoint = resolve_endpoint(endpoint)
    panes = request(endpoint, 'list')['panes']
    if target is not None:
        if not target.isascii() or not target.isdigit():
            raise ValueError('pane ID must be a decimal number')
        matches = [pane for pane in panes if pane['id'] == int(target)
                   and not pane['observe']]
        if len(matches) != 1:
            raise ValueError(f'Batty pane {target} is unavailable')
        return endpoint, matches[0]
    lineage = set(ancestors())
    matches = [pane for pane in panes if pane['pid'] in lineage
               and pane['exit_status'] is None and not pane['observe']]
    if len(matches) == 1:
        pane = matches[0]
    else:
        active = [pane for pane in panes if pane['active'] and not pane['observe']]
        if len(active) != 1:
            raise ValueError('cannot identify one active Batty pane to read aloud')
        pane = active[0]
    return endpoint, pane


def active_pane_text():
    return pane_text(*active_pane())


def pane_options(args):
    """Parse a pane action while keeping literal text separate from options."""
    values = list(args)
    endpoint = None
    target = None
    pane = False
    if values[:1] == ['--socket']:
        if len(values) < 2:
            raise ValueError('--socket needs a control endpoint')
        endpoint = values[1]
        values = values[2:]
    if values[:1] == ['--pane']:
        pane = True
        values.pop(0)
        if values and values[0].isascii() and values[0].isdigit():
            target = values.pop(0)
    return endpoint, pane, target, values


def notice(endpoint, message):
    if not endpoint:
        return
    visible = ''.join(char if char.isprintable() else ' ' for char in message)
    payload = ('Voice: ' + visible).encode('utf-8')[:255].decode('utf-8', 'ignore').encode('utf-8')
    try:
        request(endpoint, 'message', 0, payload)
    except (OSError, ValueError, RuntimeError):
        pass  # The terminal may have closed while the voice action ran.


def ensure_daemon():
    try:
        voice_request('status')
        return
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    selected = environment()
    installed = Path(selected['KILIX_VOICE_PREFIX']) / 'bin/kilix-voiced'
    if not installed.is_file():
        result = subprocess.run(['/usr/bin/env', 'bash', str(VOICE_INSTALLER),
                                 '--without-dictation'], env=selected, check=False)
        if result.returncode:
            raise ValueError('could not install the pinned read-aloud runtime')
    child = subprocess.Popen([str(installed)], env=selected, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            voice_request('status')
            return
        except (FileNotFoundError, ConnectionRefusedError):
            if child.poll() is not None:
                raise ValueError('the pinned voice daemon exited before becoming ready')
            time.sleep(0.05)
    raise ValueError('the pinned voice daemon did not become ready')


def dictate_once(seconds=30):
    selected = environment()
    session = Path(private_directory(Path(selected['KILIX_SESSION_HOME']) / 'voice'))
    receiver = session / f'cli-{os.getpid()}-{secrets.token_hex(4)}.sock'
    accepted = False
    bound = False
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listener:
        try:
            listener.bind(str(receiver))
            bound = True
            receiver.chmod(0o600)
            voice_request('dictate', sock=str(receiver))
            accepted = True
            listener.settimeout(0.25)
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline + 5:
                if time.monotonic() >= deadline and accepted:
                    voice_request('stop-dictation')
                    accepted = False
                try:
                    event = json.loads(listener.recv(65536))
                except socket.timeout:
                    continue
                if 'error' in event:
                    raise ValueError(str(event['error']))
                if 'final' in event:
                    result = ' '.join(''.join(
                        char if char.isprintable() else ' ' for char in str(event['final'])
                    ).split())
                    if not result:
                        raise ValueError('dictation heard no words')
                    return result[:16384]
            raise ValueError('dictation timed out without a final result')
        finally:
            if accepted:
                try:
                    voice_request('stop-dictation')
                except (OSError, ValueError):
                    pass
            if bound:
                receiver.unlink(missing_ok=True)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        raise ValueError('usage: kilix tts|stt [ARGS] or kilix voice status|doctor')
    tool = args.pop(0)
    if tool == 'speak':
        endpoint, pane, target, words = pane_options(args)
        if pane and words:
            raise ValueError('usage: kilix speak [--socket PATH] [--pane [ID] | TEXT...]')
        try:
            message = (pane_text(*active_pane(endpoint, target)) if pane or endpoint and not words
                       else ' '.join(words) if words else active_pane_text())
            ensure_daemon()
            voice_request('speak', text=message)
        except (OSError, ValueError, RuntimeError) as error:
            notice(endpoint, f'read-aloud failed: {error}')
            raise
        return 0
    if tool == 'dictate':
        endpoint, pane, pane_id, extra = pane_options(args)
        if extra or (endpoint and not pane):
            raise ValueError('usage: kilix dictate [--socket PATH] [--pane [ID]]')
        try:
            target = active_pane(endpoint, pane_id) if pane else None
            ensure_daemon()
            result = dictate_once()
            if target:
                endpoint, pane = target
                request(endpoint, 'paste', pane['id'], result.encode('utf-8'))
            else:
                print(result)
        except (OSError, ValueError, RuntimeError) as error:
            notice(endpoint, f'dictation failed: {error}')
            raise
        return 0
    if tool in ('tts', 'stt'):
        if args == ['--ref']:
            print(REF)
            return 0
        if args == ['--install-only']:
            print(tool_command(tool, [])[0])
            return 0
        command = tool_command(tool, args)
        selected = environment()
        # Reporting and explicit CLI actions return to the invoking shell.
        if args:
            os.execve(command[0], command, selected)
            return 0
        if inside_batty():
            launch = [str(ROOT / 'kilix'), 'launch', '--type=tab', '--self',
                      '--tab-title', 'Kilix ' + tool.upper()]
            for name in ('GPU_TERMINAL_HOME', 'KILIX_STORAGE_HOME', 'KILIX_DATA_HOME',
                         'KILIX_CONFIG_HOME', 'KILIX_SESSION_HOME', 'KILIX_STATE_DIRECTORY',
                         'KILIX_HOME', 'GPU_TERMINAL_SOURCE_HOME', 'PATH'):
                launch += ['--env', name + '=' + selected[name]]
            os.execv(launch[0], [*launch, '--', '/usr/bin/env', '-u',
                                 'KILIX_RC_PASSWORD_FILE', *command])
            return 0
        os.execve(str(ROOT / 'kilix'), [str(ROOT / 'kilix'), '--initial-title',
                                           'Kilix ' + tool.upper(), '--', *command], selected)
        return 0
    if tool == 'voice' and args in (['status'], ['doctor']):
        ready = installed_tool('tts')
        if not ready:
            print('Kilix Voice source: not installed (kilix tts --install-only)')
            return 0
        selected = environment()
        if args == ['status']:
            return subprocess.run([ready, '--status'], env=selected, check=False).returncode
        print('Kilix Voice source:', REF)
        first = subprocess.run([ready, '--status'], env=selected, check=False)
        stt = installed_tool('stt')
        if not stt:
            raise ValueError('pinned kilix-stt executable is missing')
        second = subprocess.run([stt, '--print'], env=selected, check=False)
        return first.returncode or second.returncode
    if tool == 'voice' and args and args[0] == 'install':
        if not VOICE_INSTALLER.is_file():
            raise ValueError('pinned voice installer is missing')
        return subprocess.run(['/usr/bin/env', 'bash', str(VOICE_INSTALLER),
                               *args[1:]], env=environment(), check=False).returncode
    if tool == 'voice' and args == ['daemon']:
        installed = Path(environment()['KILIX_VOICE_PREFIX']) / 'bin/kilix-voiced'
        if not installed.is_file():
            result = subprocess.run(['/usr/bin/env', 'bash', str(VOICE_INSTALLER),
                                     '--without-dictation'], env=environment(), check=False)
            if result.returncode:
                return result.returncode
        os.execve(str(installed), [str(installed)], environment())
        return 0
    if tool == 'voice' and args == ['stop']:
        try:
            voice_request('stop-speech')
            voice_request('stop-dictation')
        except FileNotFoundError:
            print('kilix voice: the voice daemon is not running')
        return 0
    if tool == 'voice' and args[:1] == ['speak'] and len(args) > 1:
        ensure_daemon()
        voice_request('speak', text=' '.join(args[1:]))
        return 0
    raise ValueError('usage: kilix tts|stt [ARGS] or kilix voice status|doctor|install|daemon|stop|speak TEXT')


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, app.InstallError) as error:
        print(f'kilix voice: {error}', file=sys.stderr)
        raise SystemExit(1)
