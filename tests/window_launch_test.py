#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Kitty-style OS-window launch opens an independent Batty Kilix frontend."""
import os
import json
from pathlib import Path
import re
import secrets
import select
import signal
import subprocess
import sys
import tempfile
import time

from Xlib import X, display

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from control import request


def wait_for(check, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.04)
    raise AssertionError('OS-window launch deadline')


with tempfile.TemporaryDirectory(prefix='bt-window-launch-') as directory:
    base = Path(directory)
    sessions = Path(os.environ['BATTY_TEST_SESSION_DIR'])
    ready = base / 'ready'
    shared_settings = base / 'shared-settings.conf'
    shared_settings.write_text('KILIX_CHROME_TAB_BAR_EDGE=top\nKILIX_CHROME_START_MENU=0\n')
    working = base / 'working directory with spaces'
    working.mkdir()
    env = os.environ | {'BATTY_WINDOW_LAUNCH_DIR': directory,
                        'BATTY_WINDOW_LAUNCH_READY': str(ready),
                        'BATTY_KILIX_CONFIG': str(ROOT / 'tests/window_launch_config.bash'),
                        'GPU_TERMINAL_SETTINGS_FILE': str(shared_settings),
                        'BATTY_TAB_BAR_EDGE': '',
                        'PYTHONDONTWRITEBYTECODE': '1',
                        'KILIX_STORAGE_HOME': str(base / 'desktop-storage'),
                        'BATTY_CONTROL_DIR': str(base / 'registry'),
                        'BATTY_SESSION_DIR': str(sessions), 'BATTY_OFFLINE': '1'}
    host = subprocess.Popen([env['BATTY_BASH'], '--noprofile', '--norc',
                             'tests/window_launch_host.bash'], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    window_endpoint = None
    new_processes = []
    orphan = None
    try:
        assert select.select([host.stdout], [], [], 10)[0], 'Host readiness deadline'
        first = int(host.stdout.readline().strip())
        endpoint = str(base / 'rw.sock')
        assert request(endpoint, 'list')['panes'][0]['id'] == first
        assert 'host_actions' not in request(endpoint, 'ping')
        try: request(endpoint, 'reload-settings')
        except RuntimeError as error: assert 'no host actions' in str(error)
        else: raise AssertionError('Generic endpoint accepted a Kilix host action')
        # An unrelated detached owner must not be taken over by the new window.
        orphan = 'kilix-auto-' + secrets.token_hex(12)
        owner = request(endpoint, 'session', 0,
                        b'\1\0' + orphan.encode() + b'\0/bin/cat\0')['id']
        request(endpoint, 'close', owner)
        wait_for(lambda: (sessions / (orphan + '.sock')).exists())
        command = [sys.executable, '-c',
                   'import os,time;print("WINDOW_ENV="+os.environ["KILIX_WINDOW_PROBE"],flush=True);'
                   'print("WINDOW_CWD="+os.getcwd(),flush=True);time.sleep(30)']
        launched = subprocess.run([str(ROOT / 'batty-kitten'), '@', '--password-file',
                                   str(base / 'unused-kitty-password'), 'launch', '--type=os-window',
                                   '--socket', endpoint, '--target', str(first), '--cwd', str(working),
                                   '--tab-title', 'Second Window', '--pane-title', 'Window Pane',
                                   '--env', 'KILIX_WINDOW_PROBE=literal value', '--', *command],
                                  env=env, capture_output=True, text=True, timeout=8)
        assert launched.returncode == 0, launched.stderr
        assert re.fullmatch(r'kilix launch: opened window \d+\n', launched.stdout)
        new_pid = int(launched.stdout.rsplit(' ', 1)[1])
        try: new_processes.append((new_pid, Path('/proc', str(new_pid), 'stat').read_text().split()[21]))
        except FileNotFoundError: pass
        wait_for(ready.exists, timeout=15)
        window_endpoint, pane_id = ready.read_text().split()
        pane_id = int(pane_id)
        assert window_endpoint != endpoint
        assert request(window_endpoint, 'ping')['host_actions'] == ['reload-settings']
        pane = request(window_endpoint, 'info', pane_id)['panes'][0]
        assert pane['page_title'] == 'Second Window' and pane['title'] == 'Window Pane'
        assert pane['persistent'] and pane['session'].startswith('kilix-auto-')
        assert len(request(window_endpoint, 'list')['panes']) == 1
        assert len(request(endpoint, 'list')['panes']) == 1
        wait_for(lambda: 'WINDOW_ENV=literal value' in request(window_endpoint, 'dump', pane_id))
        assert 'WINDOW_CWD=' + str(working) in request(window_endpoint, 'dump', pane_id)
        assert not request(window_endpoint, 'checkpoint')['appearance']['bottom_bar']
        shared_settings.write_text('KILIX_CHROME_TAB_BAR_EDGE=bottom\nKILIX_CHROME_START_MENU=1\n')
        reloaded = subprocess.run([str(ROOT / 'kilix'), 'reload-settings', '--socket', window_endpoint],
                                  env=env, capture_output=True, text=True, timeout=8)
        assert reloaded.returncode == 0, reloaded.stderr
        appearance = request(window_endpoint, 'checkpoint')['appearance']
        assert appearance['bottom_bar'] and appearance['start_badge']
        settings_client = subprocess.run([sys.executable, 'tests/bundled_settings_client.py',
                                          window_endpoint, 'top'], env=env,
                                         capture_output=True, text=True, timeout=8)
        assert settings_client.returncode == 0, settings_client.stderr
        assert 'PASS bundled desktop Settings' in settings_client.stdout
        assert not request(window_endpoint, 'checkpoint')['appearance']['bottom_bar']
        mux_main = next(p['id'] for p in request(window_endpoint, 'list')['panes']
                        if p['session'] == 'kilix-mux-main')
        windows = display.Display()
        try:
            observed = []
            def kilix_window():
                for child in windows.screen().root.query_tree().children:
                    if child.get_attributes().map_state != X.IsViewable:
                        continue
                    observed.append((child.get_wm_name(), child.get_wm_class()))
                    if child.get_wm_class() == ('batty-kilix', 'batty-kilix'):
                        return child
                return None
            try: wait_for(kilix_window)
            except AssertionError as error:
                raise AssertionError(f'{error}: {observed[-12:]}') from error
        finally:
            windows.close()
        third_ready = base / 'third-ready'
        third = subprocess.run([str(ROOT / 'batty-kitten'), '@', 'launch', '--type=os-window',
                                '--socket', endpoint, '--target', str(first), '--cwd', str(working),
                                '--tab-title', 'Third Window', '--', '/bin/cat'],
                               env=env | {'BATTY_WINDOW_LAUNCH_READY': str(third_ready)},
                               capture_output=True, text=True, timeout=8)
        assert third.returncode == 0, third.stderr
        third_pid = int(third.stdout.rsplit(' ', 1)[1])
        try: new_processes.append((third_pid, Path('/proc', str(third_pid), 'stat').read_text().split()[21]))
        except FileNotFoundError: pass
        wait_for(third_ready.exists, timeout=15)
        third_endpoint, third_pane = third_ready.read_text().split()
        third_pane = int(third_pane)
        assert third_endpoint != window_endpoint
        listed = subprocess.run([str(ROOT / 'kilix'), 'ls', '--all', '--json'], env=env,
                                capture_output=True, text=True, timeout=8)
        assert listed.returncode == 0, listed.stderr
        all_windows = json.loads(listed.stdout)['windows']
        assert len(all_windows) == 2, all_windows
        by_title = {window['panes'][0]['page_title']: window['id'] for window in all_windows}
        assert set(by_title) == {'Second Window', 'Third Window'}, by_title
        def cross(*args):
            result = subprocess.run([str(ROOT / 'kilix'), *args], env=env,
                                    capture_output=True, text=True, timeout=8)
            assert result.returncode == 0, result.stderr
        cross('rename', f'pane:{third_pane}', 'Cross-window title', '--window', by_title['Third Window'])
        assert request(third_endpoint, 'info', third_pane)['panes'][0]['page_title'] == 'Cross-window title'
        assert request(window_endpoint, 'info', pane_id)['panes'][0]['page_title'] == 'Second Window'
        xhost = display.Display()
        try:
            pid_atom = xhost.intern_atom('_NET_WM_PID')
            def top_kilix_pid():
                for child in reversed(xhost.screen().root.query_tree().children):
                    if child.get_wm_class() != ('batty-kilix', 'batty-kilix'):
                        continue
                    prop = child.get_full_property(pid_atom, X.AnyPropertyType)
                    if prop is not None and len(prop.value):
                        return int(prop.value[0])
                return None
            cross('focus', f'pane:{pane_id}', '--window', by_title['Second Window'])
            wait_for(lambda: top_kilix_pid() == new_pid)
            cross('focus', f'pane:{third_pane}', '--window', by_title['Third Window'])
            wait_for(lambda: top_kilix_pid() == third_pid)
        finally:
            xhost.close()
        third_owner = request(third_endpoint, 'info', third_pane)['panes'][0]['session']
        cross('close', f'pane:{third_pane}', '--window', by_title['Third Window'])
        wait_for(lambda: not (sessions / (third_owner + '.sock')).exists())
        assert request(window_endpoint, 'info', pane_id)['panes'][0]['exit_status'] is None
        shared_settings.unlink()
        shared_settings.mkdir()
        failed_reload = subprocess.run([str(ROOT / 'kilix'), 'reload-settings', '--socket', window_endpoint],
                                       env=env, capture_output=True, text=True, timeout=8)
        assert failed_reload.returncode != 0 and 'reload failed' in failed_reload.stderr
        shared_settings.rmdir()
        shared_settings.write_text('KILIX_CHROME_TAB_BAR_EDGE=bottom\nKILIX_CHROME_START_MENU=1\n')
        def kilix(*args):
            arguments = list(args)
            boundary = arguments.index('--') if '--' in arguments else len(arguments)
            arguments[boundary:boundary] = ['--socket', window_endpoint]
            result = subprocess.run([str(ROOT / 'kilix'), *arguments],
                                    env=env, capture_output=True, text=True, timeout=8)
            assert result.returncode == 0, result.stderr
            return result.stdout
        page_root = int(kilix('new-page', '--target', str(pane_id), '--', '/bin/cat').strip().rsplit(' ', 1)[1])
        page = request(window_endpoint, 'info', page_root)['panes'][0]['tab']
        split = int(kilix('new-pane', 'right', '--target', str(page_root), '--', '/bin/cat').strip().rsplit(' ', 1)[1])
        kilix('rename', f'tab:{page}', 'Remote page title')
        assert all(p['page_title'] == 'Remote page title' for p in request(window_endpoint, 'list')['panes']
                   if p['tab'] == page)
        split_owner = request(window_endpoint, 'info', split)['panes'][0]['session']
        kilix('close', f'pane:{split}')
        assert split not in {p['id'] for p in request(window_endpoint, 'list')['panes']}
        wait_for(lambda: not (sessions / (split_owner + '.sock')).exists())
        page_owner = request(window_endpoint, 'info', page_root)['panes'][0]['session']
        kilix('close', f'tab:{page}')
        assert not any(p['tab'] == page for p in request(window_endpoint, 'list')['panes'])
        wait_for(lambda: not (sessions / (page_owner + '.sock')).exists())
        mux_ids = []
        for action in ('serve', 'serve', 'attach', 'view'):
            output = kilix(action, 'test')
            mux_ids.append(int(re.search(r'opened pane (\d+)', output).group(1)))
        assert mux_ids[0] == mux_ids[1] == mux_ids[2] != mux_ids[3]
        mux_info = [request(window_endpoint, 'info', identity)['panes'][0] for identity in mux_ids]
        assert {pane['session'] for pane in mux_info} == {'kilix-mux-test'}
        assert len({pane['session_epoch'] for pane in mux_info}) == 1
        assert len({pane['pid'] for pane in mux_info}) == 1
        assert [pane['observe'] for pane in mux_info] == [False, False, False, True]
        assert [pane['page_title'] for pane in mux_info] == ['Mux: test'] * 3 + ['Mux: test (view)']
        foreign = subprocess.run([str(ROOT / 'kilix'), 'serve', 'test', '--socket', endpoint],
                                 env=env, capture_output=True, text=True, timeout=8)
        if foreign.returncode != 0:
            registry_state = subprocess.run([str(ROOT / 'kilix'), 'ls', '--all', '--json'],
                                            env=env, capture_output=True, text=True, timeout=8)
            registry_summary = ([(window['id'], [(p['session'], p['session_dir'])
                                                 for p in window['panes']])
                                 for window in json.loads(registry_state.stdout)['windows']]
                                if registry_state.returncode == 0 else registry_state.stderr)
            raise AssertionError((foreign.stderr, registry_summary,
                                  [(p['session'], p['session_dir'])
                                   for p in request(endpoint, 'list')['panes']]))
        assert int(re.search(r'opened pane (\d+)', foreign.stdout).group(1)) == mux_ids[0]
        assert 'window front-' in foreign.stdout
        assert all(p['session'] != 'kilix-mux-test' for p in request(endpoint, 'list')['panes'])
        assert request(window_endpoint, 'info', mux_ids[0])['panes'][0]['active']
        foreign_view = subprocess.run([str(ROOT / 'kilix'), 'view', 'test', '--socket', endpoint],
                                      env=env, capture_output=True, text=True, timeout=8)
        assert foreign_view.returncode == 0, foreign_view.stderr
        foreign_view_id = int(re.search(r'opened pane (\d+)', foreign_view.stdout).group(1))
        observed = request(endpoint, 'info', foreign_view_id)['panes'][0]
        assert observed['session'] == 'kilix-mux-test' and observed['observe']
        assert observed['session_epoch'] == mux_info[0]['session_epoch']
        request(endpoint, 'close', foreign_view_id)
        try: request(window_endpoint, 'send', mux_ids[3], b'denied')
        except RuntimeError as error: assert 'Observer pane cannot receive input' in str(error)
        else: raise AssertionError('Read-only mux view accepted input')
        assert (sessions / 'kilix-mux-test.sock').exists()
        invalid = subprocess.run([str(ROOT / 'kilix'), 'serve', '../outside', '--socket', window_endpoint],
                                 env=env, capture_output=True, text=True, timeout=8)
        assert invalid.returncode == 2
        missing = subprocess.run([str(ROOT / 'kilix'), 'attach', 'missing', '--socket', window_endpoint],
                                 env=env, capture_output=True, text=True, timeout=8)
        assert missing.returncode != 0 and not (sessions / 'kilix-mux-missing.sock').exists()
        for identity in set(mux_ids):
            request(window_endpoint, 'close', identity)
        assert (sessions / 'kilix-mux-test.sock').exists(), 'Closing a view terminated its named owner'
        resumed_output = kilix('attach', 'test')
        resumed_id = int(re.search(r'opened pane (\d+)', resumed_output).group(1))
        resumed = request(window_endpoint, 'info', resumed_id)['panes'][0]
        assert resumed['pid'] == mux_info[0]['pid']
        assert resumed['session_epoch'] == mux_info[0]['session_epoch']
        request(window_endpoint, 'close', resumed_id)
        ended = subprocess.run([str(ROOT / 'batty'), '--terminate', 'kilix-mux-test',
                                '--session-dir', str(sessions)], env=env | {'BATTY_CONFIG': '/dev/null'},
                               capture_output=True, text=True, timeout=8)
        assert ended.returncode == 0, ended.stderr
        wait_for(lambda: not (sessions / 'kilix-mux-test.sock').exists())
        request(window_endpoint, 'close', mux_main)
        assert (sessions / 'kilix-mux-main.sock').exists()
        ended_main = subprocess.run([str(ROOT / 'batty'), '--terminate', 'kilix-mux-main',
                                     '--session-dir', str(sessions)], env=env | {'BATTY_CONFIG': '/dev/null'},
                                    capture_output=True, text=True, timeout=8)
        assert ended_main.returncode == 0, ended_main.stderr
        request(window_endpoint, 'close', pane_id)
        wait_for(lambda: not (sessions / (pane['session'] + '.sock')).exists())
        request(endpoint, 'close', first)
        stdout, stderr = host.communicate(timeout=8)
        assert host.returncode == 0, (stdout, stderr)
    finally:
        for process_pid, process_start in new_processes:
            try:
                if Path('/proc', str(process_pid), 'stat').read_text().split()[21] == process_start:
                    os.killpg(process_pid, signal.SIGTERM)
            except (FileNotFoundError, ProcessLookupError):
                pass
        if host.poll() is None:
            host.terminate()
            try: host.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                host.kill(); host.communicate()
        for socket in list(sessions.glob('kilix-auto-*.sock')) + list(sessions.glob('kilix-mux-*.sock')):
            subprocess.run([str(ROOT / 'batty'), '--terminate', socket.stem, '--session-dir', str(sessions)],
                           env=env | {'BATTY_CONFIG': '/dev/null'}, capture_output=True, timeout=8)
print('PASS OS-window targeting, bundled Settings/Pane Center, cross-window native mux and cleanup')
