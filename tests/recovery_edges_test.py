#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Recovery preserves shared owners, observer roles and partially closed layouts."""
from collections import Counter
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from control import request
from kilix_auto_workspace import select_snapshot
from kilix_workspace import restore_records, save


def wait(check, process=None):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise AssertionError(f'Frontend exited: {process.returncode}')
        result = check()
        if result:
            return result
        time.sleep(0.025)
    raise AssertionError('Recovery edge deadline')


class Fixture:
    def __init__(self, base):
        self.base = base
        self.runtime = base / 'runtime'
        self.runtime.mkdir(mode=0o700)
        self.durable = base / 'state/batty/recovery'
        self.ready = base / 'ready'
        self.log = (base / 'frontend.log').open('w+')
        self.processes = []
        self.env = os.environ | {
            'BATTY_SESSION_DIR': str(self.runtime), 'BATTY_CONTROL_DIR': str(base / 'controls'),
            'BATTY_KILIX_RECOVERY_DIR': str(self.durable), 'XDG_STATE_HOME': str(base / 'state'),
            'BATTY_KILIX_CONFIG': str(ROOT / 'tests/automatic_recovery_config.bash'),
            'BATTY_AUTO_RECOVERY_READY': str(self.ready), 'BATTY_OFFLINE': '1',
            'BATTY_KILIX_AUTO_RECOVER': '0', 'BATTY_SHELL': '/bin/sh',
            'BATTY_TRANSCRIPT_ENABLED': '0', 'BATTY_TRANSCRIPT_MAINTENANCE': '0',
        }

    def command(self, *arguments, check=True):
        return subprocess.run(arguments, env=self.env, cwd=ROOT, check=check,
                              stdout=self.log, stderr=self.log, timeout=8)

    def launch(self, *arguments, automatic=False):
        self.ready.unlink(missing_ok=True)
        process = subprocess.Popen([str(ROOT / 'kilix'), *arguments], cwd=ROOT,
                                   env=self.env | {'BATTY_KILIX_AUTO_RECOVER': str(int(automatic))},
                                   stdout=self.log, stderr=self.log)
        self.processes.append(process)
        wait(self.ready.exists, process)
        return process, self.ready.read_text().strip()

    def stop(self, process):
        process.terminate()
        process.wait(timeout=5)

    def crash_owner(self, name):
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
            connection.connect(str(self.runtime / (name + '.sock')))
            pid = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
            descriptor = os.pidfd_open(pid)
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            finally:
                os.close(descriptor)
        time.sleep(0.2)

    def selected(self, listing=''):
        previous = os.environ.get('BATTY_KILIX_RECOVERY_DIR')
        os.environ['BATTY_KILIX_RECOVERY_DIR'] = str(self.durable)
        try:
            return select_snapshot(self.runtime, listing)
        finally:
            if previous is None:
                os.environ.pop('BATTY_KILIX_RECOVERY_DIR')
            else:
                os.environ['BATTY_KILIX_RECOVERY_DIR'] = previous

    def close(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for endpoint in self.runtime.glob('*.sock'):
            self.command(str(ROOT / 'batty'), '--terminate', endpoint.stem,
                         '--session-dir', str(self.runtime), check=False)
        if sys.exc_info()[0] is not None:
            self.log.flush()
            self.log.seek(0)
            print(self.log.read(), file=sys.stderr)
        self.log.close()


def shared_owner(fixture, order):
    launches = fixture.base / 'launches'
    program = fixture.base / 'program.py'
    program.write_text('import sys\n'
                       f'with open({str(launches)!r}, "a") as marker: marker.write("launch\\n")\n'
                       'print("ORIGINAL_READY", flush=True)\n'
                       'for line in sys.stdin: print(line.strip(), flush=True)\n')
    original, endpoint = fixture.launch('--session', 'shared', '--', sys.executable, '-u', str(program))
    wait(lambda: launches.exists() and len(launches.read_text().splitlines()) == 1, original)
    controller = request(endpoint, 'checkpoint')['panes'][0]
    request(endpoint, 'pane-rename', controller['id'], b'Controller')
    observer = request(endpoint, 'session', controller['id'], b'\x01\x02shared\0')['id']
    request(endpoint, 'pane-rename', observer, b'Observer')
    if order == 'observer-only':
        request(endpoint, 'close', controller['id'])
    checkpoint = request(endpoint, 'checkpoint')
    if order == 'observer-first':
        checkpoint['panes'].reverse()
    saved = fixture.base / 'shared.json'
    save(saved, checkpoint)
    fixture.stop(original)
    fixture.crash_owner('shared')

    for restart in (False, True):
        options = ('--restore', str(saved)) + (('--restart-programs',) if restart else ())
        recovered, endpoint = fixture.launch(*options)
        panes = request(endpoint, 'checkpoint')['panes']
        assert len({p['pid'] for p in panes}) == 1, panes
        assert len({p['session'] for p in panes}) == 1 and len({p['session_epoch'] for p in panes}) == 1
        assert Counter((p['title'], p['observe']) for p in panes) == Counter(
            (p['title'], p['observe']) for p in checkpoint['panes']), panes
        readonly = next(p for p in panes if p['observe'])
        try:
            request(endpoint, 'send', readonly['id'], b'NEVER_SEND\n')
        except RuntimeError:
            pass
        else:
            raise AssertionError('Recovered observer accepted input')
        if restart:
            wait(lambda: len(launches.read_text().splitlines()) >= 2, recovered)
            assert launches.read_text().splitlines() == ['launch', 'launch'], 'Program restarted once per view'
        else:
            assert launches.read_text().splitlines() == ['launch'], 'Implicit program restart'
            writable = next((p for p in panes if not p['observe']), None)
            if writable:
                request(endpoint, 'send', writable['id'], b"printf 'FRESH_%s\\n' OK\n")
                wait(lambda: 'FRESH_OK' in request(endpoint, 'dump', writable['id']), recovered)
        fixture.stop(recovered)


def partially_closed(fixture):
    original, endpoint = fixture.launch('--', '/bin/cat')
    first = request(endpoint, 'checkpoint')['panes'][0]
    request(endpoint, 'rename', first['id'], b'Keep split page')
    fixture.command(str(ROOT / 'kilix'), 'new-pane', 'right', '--socket', endpoint,
                    '--target', str(first['id']), '--', '/bin/cat')
    second = next(p for p in request(endpoint, 'checkpoint')['panes'] if p['id'] != first['id'])
    request(endpoint, 'pane-rename', second['id'], b'Surviving split')
    fixture.command(str(ROOT / 'kilix'), 'new-page', '--socket', endpoint,
                    '--target', str(second['id']), '--', '/bin/cat')
    third = next(p for p in request(endpoint, 'checkpoint')['panes'] if p['id'] not in (first['id'], second['id']))
    request(endpoint, 'rename', third['id'], b'Keep other page')
    request(endpoint, 'pane-rename', third['id'], b'Surviving page')
    for pane in (second, third):
        request(endpoint, 'send', pane['id'], b'SAVED_SURVIVOR_OUTPUT\n')
        wait(lambda: 'SAVED_SURVIVOR_OUTPUT' in request(endpoint, 'dump', pane['id']), original)
    fixture.durable.mkdir(mode=0o700, parents=True)
    saved = fixture.durable / ('.kilix-layout-' + 'a' * 24 + '.json')
    save(saved, request(endpoint, 'checkpoint'))
    fixture.stop(original)
    # This is the last durable checkpoint when a pane closes immediately before
    # a crash: its closure is durable but no replacement layout has been saved.
    fixture.command(str(ROOT / 'batty'), '--terminate', first['session'], '--session-dir', str(fixture.runtime))
    for pane in (second, third):
        fixture.crash_owner(pane['session'])
    shutil.rmtree(fixture.runtime)
    assert fixture.selected() == saved and saved.exists()
    assert len(list(fixture.durable.glob('.batty-output-*/*.bt-output'))) == 3
    recovered, endpoint = fixture.launch(automatic=True)
    panes = request(endpoint, 'checkpoint')['panes']
    assert len(panes) == 2 and {p['title'] for p in panes} == {'Surviving split', 'Surviving page'}
    assert {p['page_title'] for p in panes} == {'Keep split page', 'Keep other page'}
    assert len({p['tab'] for p in panes}) == 2
    for pane in panes:
        assert 'SAVED_SURVIVOR_OUTPUT' in request(endpoint, 'dump', pane['id'])
    def replacement_saved():
        for path in fixture.durable.glob('.kilix-layout-*.json'):
            if path != saved and len(restore_records(path, return_document=True)['panes']) == 2:
                return path
        return None
    replacement = wait(replacement_saved, recovered)
    wait(lambda: not saved.exists(), recovered)
    fixture.stop(recovered)
    for pane in panes:
        fixture.command(str(ROOT / 'batty'), '--terminate', pane['session'], '--session-dir', str(fixture.runtime))
    assert fixture.selected() is None and not replacement.exists()
    assert not list(fixture.durable.glob('.batty-output-*/*.bt-output'))


def owner_crash(fixture):
    original, endpoint = fixture.launch('--', '/bin/cat')
    pane = request(endpoint, 'checkpoint')['panes'][0]
    request(endpoint, 'rename', pane['id'], b'Owner crash page')
    request(endpoint, 'pane-rename', pane['id'], b'Owner crash output')
    request(endpoint, 'send', pane['id'], b'ARCHIVED_BEFORE_CRASH\n')
    wait(lambda: 'ARCHIVED_BEFORE_CRASH' in request(endpoint, 'dump', pane['id']), original)
    fixture.durable.mkdir(mode=0o700, parents=True)
    saved = fixture.durable / ('.kilix-layout-' + 'b' * 24 + '.json')
    save(saved, request(endpoint, 'checkpoint'))
    fixture.stop(original)
    fixture.crash_owner(pane['session'])
    assert (fixture.runtime / (pane['session'] + '.sock')).exists(), 'Missing stale-socket fixture'
    listing = subprocess.check_output([str(ROOT / 'batty'), '--list', '--session-dir', str(fixture.runtime)],
                                      env=fixture.env, text=True, timeout=8)
    assert pane['session'] + ' unavailable' in listing, listing
    assert fixture.selected(listing) == saved, 'Unavailable owner blocked durable recovery'
    recovered, endpoint = fixture.launch(automatic=True)
    panes = request(endpoint, 'checkpoint')['panes']
    assert len(panes) == 1 and panes[0]['title'] == 'Owner crash output'
    restored = panes[0]
    assert restored['page_title'] == 'Owner crash page' and not restored['observe']
    assert restored['session_epoch'] != pane['session_epoch'] and restored['pid'] != pane['pid']
    assert 'ARCHIVED_BEFORE_CRASH' in request(endpoint, 'dump', restored['id'])
    request(endpoint, 'send', restored['id'], b"printf 'FRESH_%s\\n' OK\n")
    wait(lambda: 'FRESH_OK' in request(endpoint, 'dump', restored['id']), recovered)


def automatic_observer(fixture, reboot):
    original, endpoint = fixture.launch('--', '/bin/cat')
    owner = request(endpoint, 'checkpoint')['panes'][0]
    request(endpoint, 'send', owner['id'], b'OBSERVED_BEFORE_RECOVERY\n')
    wait(lambda: 'OBSERVED_BEFORE_RECOVERY' in request(endpoint, 'dump', owner['id']), original)
    spectator, endpoint = fixture.launch('--observe', owner['session'])
    observer = request(endpoint, 'checkpoint')['panes'][0]
    request(endpoint, 'rename', observer['id'], b'Observer page')
    request(endpoint, 'pane-rename', observer['id'], b'Saved observer')
    fixture.durable.mkdir(mode=0o700, parents=True)
    saved = fixture.durable / ('.kilix-layout-' + 'c' * 24 + '.json')
    save(saved, request(endpoint, 'checkpoint'))
    fixture.stop(original)
    fixture.stop(spectator)
    if reboot:
        fixture.crash_owner(owner['session'])
        shutil.rmtree(fixture.runtime)
    else:
        # An unrelated orphan must still be recovered after the saved layout.
        unrelated, endpoint = fixture.launch('--', '/bin/cat')
        extra = request(endpoint, 'checkpoint')['panes'][0]
        fixture.stop(unrelated)
    recovered, endpoint = fixture.launch(automatic=True)
    panes = request(endpoint, 'checkpoint')['panes']
    restored = [pane for pane in panes if pane['title'] == 'Saved observer']
    assert len(restored) == 1 and restored[0]['observe'], panes
    restored = restored[0]
    assert restored['page_title'] == 'Observer page'
    assert 'OBSERVED_BEFORE_RECOVERY' in request(endpoint, 'dump', restored['id'])
    assert [pane['observe'] for pane in panes if pane['session'] == restored['session']] == [True], \
        'Orphan recovery added a controller to an already restored observer'
    try:
        request(endpoint, 'send', restored['id'], b'NEVER_SEND\n')
    except RuntimeError:
        pass
    else:
        raise AssertionError('Automatically recovered observer accepted input')
    if reboot:
        assert len(panes) == 1 and restored['session_epoch'] != owner['session_epoch']
        controller, writable_endpoint = fixture.launch('--attach', restored['session'])
        writable = request(writable_endpoint, 'checkpoint')['panes'][0]
        request(writable_endpoint, 'send', writable['id'], b"printf 'FRESH_%s\\n' OK\n")
        wait(lambda: 'FRESH_OK' in request(endpoint, 'dump', restored['id']), controller)
    else:
        assert len(panes) == 2 and restored['pid'] == owner['pid']
        other = next(pane for pane in panes if pane['session'] == extra['session'])
        assert other['pid'] == extra['pid'] and not other['observe']


for scenario in ('controller-first', 'observer-first', 'observer-only', 'partially-closed',
                 'owner-crash', 'automatic-observer-live', 'automatic-observer-reboot'):
    with tempfile.TemporaryDirectory(prefix='bt-recovery-edges-') as directory:
        fixture = Fixture(Path(directory))
        try:
            if scenario == 'partially-closed':
                partially_closed(fixture)
            elif scenario == 'owner-crash':
                owner_crash(fixture)
            elif scenario.startswith('automatic-observer-'):
                automatic_observer(fixture, scenario.endswith('-reboot'))
            else:
                shared_owner(fixture, scenario)
            print('PASS recovery edge: ' + scenario, flush=True)
        finally:
            fixture.close()
