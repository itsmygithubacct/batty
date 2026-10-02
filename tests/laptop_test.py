#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise laptop profile lifecycle in isolated Batty windows."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from control import request  # noqa: E402


def wait_for(check, deadline=12):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        found = check()
        if found:
            return found
        time.sleep(.05)
    raise AssertionError('laptop window deadline')


with tempfile.TemporaryDirectory(prefix='bt-laptop-') as temporary:
    base = Path(temporary)
    profiles = base / 'profiles'
    profiles.mkdir(mode=0o700)
    for name, layout in (('splits', 'splits'), ('tabs', 'tabs')):
        (profiles / (name + '.profile')).write_text(
            f'name={name.title()} Laptop\nlayout={layout}\n'
            'pane.1.title=First\npane.1.cmd=cat\n'
            'pane.2.title=Second\npane.2.cmd=cat\n')
    (profiles / 'invalid.profile').write_text('pane.1.cmd=cat\npane.3.cmd=cat\n')
    (profiles / 'xp.profile').write_text('desktop=xp\n')
    (profiles / 'tui.profile').write_text('desktop=tui\n')
    (profiles / 'cap.profile').write_text('desktop=cap\n')
    (profiles / 'land.profile').write_text('desktop=land\n')
    (profiles / 'icewm.profile').write_text('desktop=icewm\n')
    env = os.environ | {'KILIX_LAPTOP_PROFILES': str(profiles),
                        'BATTY_KILIX_STORAGE_HOME': str(base / 'storage'),
                        'XDG_STATE_HOME': str(base / 'state'),
                        'BATTY_OFFLINE': '1'}

    def cli(*args):
        return subprocess.run([str(ROOT / 'kilix'), 'laptop', *args], env=env,
                              capture_output=True, text=True, timeout=40)

    assert 'open PROFILE' in cli('help').stdout
    assert cli('list').stdout.splitlines() == ['cap', 'icewm', 'invalid', 'land', 'splits', 'tabs', 'tui', 'xp']
    assert 'invalid invalid' in cli('status').stdout
    assert 'xp desktop' in cli('status').stdout
    assert 'tui desktop' in cli('status').stdout
    assert 'cap desktop' in cli('status').stdout
    assert 'land desktop' in cli('status').stdout
    assert 'icewm desktop' in cli('status').stdout
    assert cli('open', 'invalid').returncode != 0
    sleeper = subprocess.Popen(['/bin/sleep', '30'])
    try:
        stale = profiles / 'run/tabs.pid'
        stale.parent.mkdir(mode=0o700, exist_ok=True)
        stale.write_text(f'{sleeper.pid}\nboot_id=00000000-0000-0000-0000-000000000000\n'
                         'start_time=1\n')
        assert 'tabs stopped' in cli('status').stdout
        assert not stale.exists() and sleeper.poll() is None
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)
    try:
        for name in ('splits', 'tabs'):
            opened = cli('open', name)
            assert opened.returncode == 0, opened.stderr
            record = profiles / 'run' / (name + '.pid')
            pid = int(record.read_text().splitlines()[0])
            assert f'{name} running (pid {pid})' in cli('status').stdout
            assert 'already running' in cli('open', name).stdout
            socket = wait_for(lambda: next((profiles / 'run' / (name + '.control'))
                                           .glob('front-*/control.sock'), None))
            panes = request(str(socket), 'list')['panes']
            assert len(panes) == 2
            assert (panes[0]['tab'] == panes[1]['tab']) == (name == 'splits')
            if name == 'splits':
                assert {p['page_title'] for p in panes} == {'Splits Laptop'}
            else:
                assert {p['page_title'] for p in panes} == {'First', 'Second'}
            closed = cli('close', name)
            assert closed.returncode == 0, closed.stderr
            assert f'{name} stopped' in cli('status').stdout
            wait_for(lambda: not Path('/proc', str(pid)).exists())
            wait_for(lambda: not list((profiles / 'run' / (name + '.sessions')).glob('*.sock')))
    finally:
        for name in ('splits', 'tabs'):
            cli('close', name)

print('PASS laptop profiles: strict parse, separate windows, split/tab layouts, live registry, safe close')
