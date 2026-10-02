#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Kilix shortcut close requires explicit confirmation for a live owner."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from Xlib import X, XK, display
from Xlib.ext import xtest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from control import request


def wait_for(check, process=None):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        if process is not None and process.poll() is not None:
            raise AssertionError(f'Frontend exited before confirmation: {process.returncode}')
        time.sleep(0.03)
    raise AssertionError('Close confirmation deadline')


def owner_identities(panes):
    return [(p['id'], p['pid'], p['session_epoch'], p['observe'], p['exit_status'], p['disconnected'])
            for p in panes]


with tempfile.TemporaryDirectory(prefix='bt-close-confirm-') as directory:
    base = Path(directory)
    sessions = base / 'sessions'
    ready = base / 'ready'
    recovery = base / 'recovery'
    env = os.environ | {'BATTY_SESSION_DIR': str(sessions),
                        'BATTY_KILIX_RECOVERY_DIR': str(recovery),
                        'BATTY_KILIX_CONFIG': str(ROOT / 'tests/automatic_recovery_config.bash'),
                        'BATTY_AUTO_RECOVERY_READY': str(ready),
                        'BATTY_KILIX_AUTO_RECOVER': '0', 'BATTY_OFFLINE': '1'}
    with (base / 'frontend.log').open('w+') as log:
        process = subprocess.Popen([str(ROOT / 'kilix'), '--', '/bin/cat'],
                                   env=env, stdout=log, stderr=log)
        host = None
        try:
            wait_for(ready.exists, process)
            endpoint = ready.read_text().strip()
            pane = request(endpoint, 'checkpoint')['panes'][0]
            owner = sessions / (pane['session'] + '.sock')
            assert owner.exists() and pane['persistent']
            host = display.Display()

            def window():
                for candidate in host.screen().root.query_tree().children:
                    if (candidate.get_attributes().map_state == X.IsViewable and
                            candidate.get_wm_class() == ('batty-kilix', 'batty-kilix')):
                        return candidate
                return None

            target = wait_for(window, process)
            target.set_input_focus(X.RevertToParent, X.CurrentTime)
            host.sync()

            def key(name):
                code = host.keysym_to_keycode(XK.string_to_keysym(name))
                assert code, name
                xtest.fake_input(host, X.KeyPress, code)
                xtest.fake_input(host, X.KeyRelease, code)
                host.sync()

            def close_shortcut():
                control = host.keysym_to_keycode(XK.string_to_keysym('Control_L'))
                shift = host.keysym_to_keycode(XK.string_to_keysym('Shift_L'))
                letter = host.keysym_to_keycode(XK.string_to_keysym('w'))
                for code in (control, shift, letter):
                    xtest.fake_input(host, X.KeyPress, code)
                for code in (letter, shift, control):
                    xtest.fake_input(host, X.KeyRelease, code)
                host.sync()

            close_shortcut()
            time.sleep(0.25)
            assert process.poll() is None and owner.exists(), 'Shortcut terminated owner before confirmation'
            key('n')
            time.sleep(0.15)
            assert process.poll() is None and owner.exists(), 'N failed to cancel termination'
            close_shortcut()
            time.sleep(0.15)
            recovery.chmod(0o755)
            key('y')
            wait_for(lambda: 'record recovery closure' in (base / 'frontend.log').read_text(), process)
            assert owner.exists() and owner_identities(request(endpoint, 'checkpoint')['panes']) == owner_identities([pane]), \
                'Failed durable closure removed its pane or owner'
            recovery.chmod(0o700)
            key('Escape')  # Dismiss the action-failed message before retrying.
            close_shortcut()
            time.sleep(0.15)
            key('y')
            wait_for(lambda: process.poll() is not None)
            assert process.returncode == 0 and not owner.exists(), 'Y did not terminate the owner'
            ready.unlink()
            process = subprocess.Popen([str(ROOT / 'kilix'), '--', '/bin/cat'],
                                       env=env | {'BATTY_KILIX_CONFIG': str(ROOT / 'tests/close_confirmation_page_config.bash')},
                                       stdout=log, stderr=log)
            wait_for(ready.exists, process)
            endpoint = ready.read_text().strip()
            panes = request(endpoint, 'checkpoint')['panes']
            assert len(panes) == 2 and all(p['persistent'] for p in panes)
            owners = [sessions / (pane['session'] + '.sock') for pane in panes]
            assert all(path.exists() for path in owners)
            target = wait_for(window, process)
            target.set_input_focus(X.RevertToParent, X.CurrentTime)
            host.sync()
            assert process.poll() is None, 'Page close skipped confirmation'
            failures = (base / 'frontend.log').read_text().count('record recovery closure')
            recovery.chmod(0o755)
            key('y')
            wait_for(lambda: (base / 'frontend.log').read_text().count('record recovery closure') > failures, process)
            assert all(path.exists() for path in owners) and owner_identities(request(endpoint, 'checkpoint')['panes']) == owner_identities(panes), \
                'Failed durable page closure removed its panes or owners'
            recovery.chmod(0o700)
            # The first owner now closes successfully, but the second one's
            # marker rejects closure. A retry must remember the first success.
            marker = recovery / ('closed-' + panes[1]['session_epoch'])
            marker.write_bytes(b'')
            marker.chmod(0o644)
            key('Escape')
            key('F9')
            time.sleep(0.15)
            key('y')
            failures += 1
            wait_for(lambda: (base / 'frontend.log').read_text().count('record recovery closure') > failures, process)
            assert not owners[0].exists() and owners[1].exists(), 'Partial page closure did not retain the failing owner'
            assert [p['id'] for p in request(endpoint, 'checkpoint')['panes']] == [p['id'] for p in panes], \
                'Partial page closure removed saved output panes'
            marker.chmod(0o600)
            key('Escape')
            key('F9')
            time.sleep(0.15)
            key('y')
            wait_for(lambda: process.poll() is not None)
            assert process.returncode == 0 and all(not path.exists() for path in owners), \
                'Confirmed page close left a generated owner running'
            print('PASS live Kilix pane/page close requires Y; N and storage failure preserve owners and panes; partial closure retries after storage repair')
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read(), file=sys.stderr)
            raise
        finally:
            if recovery.exists():
                recovery.chmod(0o700)
            if host is not None:
                host.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for socket in sessions.glob('kilix-auto-*.sock'):
                subprocess.run([str(ROOT / 'batty'), '--terminate', socket.stem,
                                '--session-dir', str(sessions)], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
