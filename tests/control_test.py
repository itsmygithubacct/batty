#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""External workspace control against live PTYs and an isolated display."""
import json
import os
import re
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from control import apply_layout, request, events


def expect_error(call, phrase):
    try:
        call()
    except (RuntimeError, ValueError) as error:
        assert phrase in str(error), str(error)
    else:
        raise AssertionError('Expected failure: ' + phrase)


with tempfile.TemporaryDirectory(prefix='bt-control-') as directory:
    private = Path(directory)
    (private / 'unsafe').mkdir(mode=0o755)
    (private / 'unsafe').chmod(0o755)
    (private / 'claude-config' / 'sessions').mkdir(parents=True)
    env = os.environ | {'BATTY_CONTROL_TEST_DIR': directory,
                        'CLAUDE_CONFIG_DIR': str(private / 'claude-config'),
                        'BATTY_KILIX_STORAGE_HOME': str(private / 'storage')}
    process = subprocess.Popen([env['BATTY_BASH'], '--noprofile', '--norc', 'tests/control.bash'],
                               cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stopped_owner = 0
    pending_dump = None
    try:
        assert select.select([process.stdout], [], [], 10)[0], 'Controller readiness deadline'
        line = process.stdout.readline()
        assert line.strip(), process.stderr.read()
        first, observer = map(int, line.split())
        rw, ro = str(private / 'rw.sock'), str(private / 'ro.sock')
        def wait_detaches():
            deadline = time.monotonic() + 5
            while request(rw, 'ping')['pending_detaches']:
                assert time.monotonic() < deadline, 'Background detach deadline'
                time.sleep(0.01)

        def close_persistent(pane):
            request(rw, 'close', pane)
            wait_detaches()

        assert (private / 'rw.sock').stat().st_mode & 0o777 == 0o600
        assert not (private / 'unsafe/socket').exists()
        assert request(rw, 'ping')['version'] == 1
        assert request(ro, 'ping')['read_only']
        assert 'font-size' in request(rw, 'ping')['operations']
        assert 'font-size' not in request(ro, 'ping')['operations']
        expect_error(lambda: request(ro, 'font-size', payload=b'\x12'), 'read-only')
        expect_error(lambda: request(rw, 'font-size', first, b'\x12'), 'Malformed')
        expect_error(lambda: request(rw, 'font-size', payload=b'\x05'), 'Malformed')
        expect_error(lambda: request(rw, 'font-size', payload=b'\x12\x00'), 'Malformed')
        assert request(rw, 'font-size', payload=b'\x12') == {}
        assert request(rw, 'checkpoint')['appearance']['font_size'] == 18
        assert request(ro, 'checkpoint')['appearance']['font_size'] == 16
        screen = [str(ROOT / 'kilix'), 'screen-size', '--socket', rw]
        screen_env = env | {'BATTY_CONTROL_DIR': str(private / 'absent-registry')}
        changed = subprocess.run(screen + ['set', '20'], env=screen_env, capture_output=True, text=True, timeout=5)
        assert changed.returncode == 0 and changed.stdout.strip() == 'font_size 20', changed.stderr
        assert (private / 'storage/config/font-size').read_text() == '20\n'
        scaled = request(rw, 'checkpoint')
        assert scaled['appearance']['font_size'] == 20
        assert bytes.fromhex(scaled['layout_hex'])[16] == 20
        shown = subprocess.run(screen + ['show'], env=screen_env, capture_output=True, text=True, timeout=5)
        assert shown.returncode == 0 and shown.stdout.strip() == 'font_size 20'
        assert request(rw, 'font-size', payload=b'\x10') == {}
        for endpoint in (rw, ro):
            assert 'checkpoint' in request(endpoint, 'ping')['operations']
            saved = request(endpoint, 'checkpoint')
            raw = bytes.fromhex(saved['layout_hex'])
            assert saved['version'] == 1 and saved['layout_format'] == 'BWL2' and raw[:4] == b'BWL2'
            assert raw[5] == len(saved['panes'])
            assert {struct.unpack_from('<Q', raw, 8 + i * 10)[0] for i in range(raw[5])} == {p['id'] for p in saved['panes']}
            assert saved['appearance']['width'] > 0 and saved['appearance']['height'] > 0
            assert saved['appearance']['font_size'] >= 6 and saved['appearance']['font']
        expect_error(lambda: request(rw, 'checkpoint', first), 'Malformed')
        expect_error(lambda: request(rw, 'checkpoint', 0, b'extra'), 'Malformed')
        cli_checkpoint = subprocess.run([str(ROOT / 'battyctl'), '--socket', rw, 'checkpoint'], env=env,
                                        capture_output=True, text=True, timeout=5)
        assert cli_checkpoint.returncode == 0, cli_checkpoint.stderr
        assert json.loads(cli_checkpoint.stdout)['layout_format'] == 'BWL2'
        save_file = private / 'workspace.json'
        refused_save = subprocess.run([str(ROOT / 'kilix'), 'save', '--socket', rw, str(save_file)], env=env,
                                     capture_output=True, text=True, timeout=5)
        assert refused_save.returncode != 0 and 'named persistent session' in refused_save.stderr and not save_file.exists()
        saved_endpoint = str(private / 'saved.sock')
        save_command = [str(ROOT / 'kilix'), 'save', '--socket', saved_endpoint, str(save_file)]
        for _ in range(2):
            result = subprocess.run(save_command, env=env, capture_output=True, text=True, timeout=5)
            assert result.returncode == 0, result.stderr
            assert save_file.stat().st_mode & 0o777 == 0o600
            document = json.loads(save_file.read_text())
            assert document['format'] == 'batty-workspace' and document['version'] == 2
            checkpoint = document['checkpoint']
            assert checkpoint['appearance']['width'] == 500 and checkpoint['appearance']['height'] == 300
            assert len(checkpoint['panes']) == 1
            descriptor = checkpoint['panes'][0]
            assert descriptor['session'] == 'saved-workspace' and descriptor['session_dir'] == env['BATTY_TEST_SESSION_DIR']
            assert int(descriptor['session_epoch'], 16) and descriptor['persistent']
        pty_command = [str(ROOT / 'kilix'), 'pty', '--session-dir', env['BATTY_TEST_SESSION_DIR']]
        listed = subprocess.run([*pty_command, '--json'], env=env,
                                capture_output=True, text=True, timeout=5)
        assert listed.returncode == 0, listed.stderr
        owners = json.loads(listed.stdout)
        assert owners['schema'] == 'batty.pty-sessions/v1'
        assert any(item['name'] == 'saved-workspace' and item['available'] and
                   item['controllers'] == 1 for item in owners['sessions'])
        attached = subprocess.run([*pty_command, '--socket', saved_endpoint,
                                   'attach', 'saved-workspace'], env=env,
                                  capture_output=True, text=True, timeout=5)
        assert attached.returncode == 0 and 'Focused pane' in attached.stdout, attached.stderr
        viewed = subprocess.run([*pty_command, '--socket', rw, 'view', 'saved-workspace'],
                                env=env, capture_output=True, text=True, timeout=5)
        assert viewed.returncode == 0 and 'Opened pane' in viewed.stdout, viewed.stderr
        observer_pane = next(pane['id'] for pane in request(rw, 'list')['panes']
                             if pane['session'] == 'saved-workspace' and pane['observe'])
        assert observer_pane != first
        request(rw, 'close', observer_pane)
        wait_detaches()
        original_file = save_file.read_bytes()
        link = private / 'workspace-link.json'
        link.symlink_to(save_file)
        rejected_link = subprocess.run(save_command[:-1] + [str(link)], env=env, capture_output=True, text=True, timeout=5)
        assert rejected_link.returncode != 0 and link.is_symlink() and save_file.read_bytes() == original_file
        from unittest.mock import patch
        from kilix_workspace import save
        restricted_file = private / 'restricted-workspace.json'
        prior_umask = os.umask(0o777)
        try:
            save(restricted_file, checkpoint)
        finally:
            os.umask(prior_umask)
        assert restricted_file.stat().st_mode & 0o777 == 0o600
        with patch('kilix_workspace.os.replace', side_effect=OSError('injected replace failure')):
            try:
                save(save_file, checkpoint)
            except OSError:
                pass
            else:
                raise AssertionError('Injected failed save succeeded')
        assert save_file.read_bytes() == original_file and not list(private.glob('.batty-workspace-*'))


        panes = request(rw, 'list')['panes']
        assert len(panes) == 1 and panes[0]['id'] == first and panes[0]['pid'] > 0
        assert panes[0]['recording'] == 'disabled'
        assert 'message' in request(rw, 'ping')['operations']
        assert 'message' not in request(ro, 'ping')['operations']
        assert request(rw, 'message', 0, b'Voice ready') == {}
        assert request(rw, 'message', 0, b'') == {}
        expect_error(lambda: request(ro, 'message', 0, b'forbidden'), 'read-only')
        expect_error(lambda: request(rw, 'message', 0, b'bad\nmessage'), 'printable')
        expect_error(lambda: request(rw, 'message', first, b'wrong scope'), 'Malformed')
        assert all(p['recording'] == 'disabled' for p in request(saved_endpoint, 'list')['panes'])
        expect_error(lambda: request(ro, 'send', observer, b'forbidden'), 'read-only')
        expect_error(lambda: request(ro, 'launch', 0, b'\1/bin/true\0'), 'read-only')
        expect_error(lambda: request(rw, 'focus', observer), 'does not exist')
        assert len(request(ro, 'list')['panes']) == 1
        request(rw, 'send', first, b'EXTERNAL_INPUT\n')
        deadline = time.monotonic() + 5
        while 'EXTERNAL_INPUT' not in request(rw, 'dump', first):
            assert time.monotonic() < deadline
        launched = request(rw, 'launch', first, b'\1/bin/cat\0')['id']
        assert launched != first
        scope_view = str(private / 'scope-view.sock')
        scope_input = str(private / 'scope-input.sock')
        scoped_cursors = {}
        for endpoint, writable in ((scope_view, False), (scope_input, True)):
            capabilities = request(endpoint, 'ping')
            assert capabilities['scope_pane'] == first and capabilities['read_only'] != writable
            assert set(capabilities['operations']) == (
                {'ping', 'list', 'dump', 'info', 'events'} |
                ({'send', 'paste'} if writable else set()))
            assert [pane['id'] for pane in request(endpoint, 'list')['panes']] == [first]
            assert request(endpoint, 'info', first)['panes'][0]['id'] == first
            expect_error(lambda: request(endpoint, 'info', launched), 'outside endpoint scope')
            expect_error(lambda: request(endpoint, 'dump', launched), 'outside endpoint scope')
            expect_error(lambda: request(endpoint, 'checkpoint'), 'outside pane scope')
            expect_error(lambda: request(endpoint, 'pane-center-state'), 'outside pane scope')
            denied = 'outside pane scope' if writable else 'read-only'
            expect_error(lambda: request(endpoint, 'font-size', payload=b'\x12'), denied)
            expect_error(lambda: request(endpoint, 'launch', first, b'\1/bin/true\0'), denied)
            expect_error(lambda: request(endpoint, 'close', first), denied)
            initial = events(endpoint)
            assert initial['reset'] and [pane['id'] for pane in initial['panes']] == [first]
            assert not initial['events']
            scoped_cursors[endpoint] = initial
        expect_error(lambda: request(scope_view, 'send', first, b'forbidden'), 'read-only')
        expect_error(lambda: request(scope_input, 'send', launched, b'forbidden'), 'outside endpoint scope')
        request(scope_input, 'send', first, b'SCOPED_INPUT\n')
        request(scope_input, 'paste', first, b'SCOPED_PASTE\n')
        deadline = time.monotonic() + 5
        while 'SCOPED_PASTE' not in request(scope_view, 'dump', first):
            assert time.monotonic() < deadline, 'Scoped input deadline'
        assert 'SCOPED_INPUT' in request(scope_view, 'dump', first)
        request(rw, 'send', launched, b'OTHER_PANE_INPUT\n')
        for endpoint, initial in scoped_cursors.items():
            changed = events(endpoint, initial['cursor'], initial['epoch'])
            assert not changed['reset'] and changed['events']
            assert {event['pane'] for event in changed['events']} == {first}
        assert 'pane-center' in request(rw, 'ping')['operations']
        assert 'pane-center' not in request(ro, 'ping')['operations']
        expect_error(lambda: request(ro, 'pane-center'), 'read-only')
        expect_error(lambda: request(rw, 'pane-center', first), 'Malformed')
        expect_error(lambda: request(rw, 'pane-center', 0, b'extra'), 'Malformed')
        assert request(rw, 'pane-center')['open']
        first_center = request(rw, 'pane-center-state')
        panes_cli = [str(ROOT / 'kilix'), 'panes', '--socket', rw]
        opened = subprocess.run(panes_cli, env=env, capture_output=True, text=True, timeout=5)
        assert opened.returncode == 0 and not opened.stderr, opened.stderr
        second_center = request(rw, 'pane-center-state')
        joined = subprocess.run([*panes_cli, '--json'], env=env, capture_output=True, text=True, timeout=5)
        assert joined.returncode == 0, joined.stderr
        snapshot = json.loads(joined.stdout)
        assert snapshot['schema'] == 'kilix.panes/v1' and snapshot['counts'] == {'pages': 1, 'panes': 2}
        assert {p['pane_id'] for p in snapshot['panes']} == {first, launched}
        assert all(p['coding_session'] is None and p['activity'] == 'running' and
                   any(member['pid'] == p['process']['child_pid'] for member in p['process']['foreground'])
                   for p in snapshot['panes']), snapshot['panes']
        saved_layout = request(rw, 'checkpoint')
        identity_map = [(pane['id'], pane['id']) for pane in saved_layout['panes']]
        assert 'layout-apply' in request(rw, 'ping')['operations']
        assert 'layout-apply' not in request(ro, 'ping')['operations']
        expect_error(lambda: apply_layout(ro, saved_layout['layout_hex'], identity_map), 'read-only')
        expect_error(lambda: request(rw, 'layout-apply', payload=b'bad'), 'Malformed control request')
        request(rw, 'rename', first, b'Temporary page')
        assert request(rw, 'checkpoint')['layout_hex'] != saved_layout['layout_hex']
        apply_layout(rw, saved_layout['layout_hex'], identity_map)
        assert request(rw, 'checkpoint')['layout_hex'] == saved_layout['layout_hex']
        request(rw, 'pane-center')
        center = request(rw, 'pane-center-state')
        assert center['open'] and set(center['ids']) == {first, launched}, (first_center, second_center, center)
        assert not request(ro, 'pane-center-state')['open']
        assert 'telemetry' in request(rw, 'ping')['operations']
        assert 'telemetry' not in request(ro, 'ping')['operations']
        expect_error(lambda: request(ro, 'telemetry', first, b'working\0codex\0'), 'read-only')
        expect_error(lambda: request(rw, 'telemetry', first, b'bad\0codex\0'), 'Invalid pane telemetry')
        expect_error(lambda: request(rw, 'telemetry', first, b'working\0bad\x1bname\0'), 'Invalid pane telemetry')
        request(rw, 'telemetry', first, b'working\0codex\0codex 12345678\0Build Pane Center task display')
        assert request(rw, 'info', first)['panes'][0]['overlay_task'] == 'Build Pane Center task display'
        expect_error(lambda: request(rw, 'telemetry', first, b'working\0codex\0agent\0bad\x1btask'),
                     'Invalid pane telemetry')
        helper = subprocess.Popen([sys.executable, '-B', str(ROOT / 'tools/kilix_overlay.py')],
                                  cwd=ROOT, env=env | {'BATTY_CONTROL': rw},
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while True:
                label = request(rw, 'info', launched)['panes'][0]
                if label['overlay_activity'] == 'running' and label['overlay_process'] == 'cat':
                    break
                assert helper.poll() is None, helper.stderr.read()
                assert time.monotonic() < deadline, label
                time.sleep(0.05)
        finally:
            helper.terminate()
            helper.wait(timeout=3)
            helper.stderr.close()
        active_pane = next(p['id'] for p in request(rw, 'list')['panes'] if p['active'])
        request(rw, 'focus', first if active_pane != first else launched)
        assert not request(rw, 'pane-center-state')['open']
        request(rw, 'focus', first)
        no_agent = subprocess.run([*panes_cli, 'wait', str(launched), '--for', 'idle', '--timeout', '1'],
                                  env=env, capture_output=True, text=True, timeout=5)
        assert no_agent.returncode != 0 and 'requires a live Codex rollout owner' in no_agent.stderr
        nested = request(rw, 'launch', first, b'\1/bin/sh\0-c\0sleep 15; :\0')['id']
        deadline = time.monotonic() + 5
        while True:
            nested_snapshot = subprocess.run([*panes_cli, '--json'], env=env,
                                             capture_output=True, text=True, timeout=5)
            assert nested_snapshot.returncode == 0, nested_snapshot.stderr
            nested_pane = next(p for p in json.loads(nested_snapshot.stdout)['panes']
                               if p['pane_id'] == nested)
            if len(nested_pane['process']['foreground']) >= 2:
                assert nested_pane['process']['name'] == 'sleep'
                assert nested_pane['activity'] == 'running'
                break
            assert time.monotonic() < deadline, nested_pane
            time.sleep(0.02)
        request(rw, 'close', nested)
        rollout = private / 'rollout-2026-09-23-12345678-1234-1234-1234-123456789abc.jsonl'
        agent = private / 'codex'
        agent.write_text('#!/usr/bin/python3\n'
                         'import json,sys,time\n'
                         'with open(sys.argv[1], "a") as stream:\n'
                         '    for event in ("task_started", "task_complete"):\n'
                         '        stream.write(json.dumps({"type":"event_msg","payload":{"type":event}})+"\\n")\n'
                         '        stream.flush()\n'
                         '        time.sleep(0.8)\n'
                         '    time.sleep(3)\n')
        agent.chmod(0o700)
        agent_pane = request(rw, 'launch', first,
                             b'\1' + str(agent).encode() + b'\0' + str(rollout).encode() + b'\0')['id']
        deadline = time.monotonic() + 5
        while True:
            agent_snapshot = subprocess.run([*panes_cli, '--json'], env=env,
                                            capture_output=True, text=True, timeout=5)
            assert agent_snapshot.returncode == 0, agent_snapshot.stderr
            agent_record = next(p for p in json.loads(agent_snapshot.stdout)['panes']
                                if p['pane_id'] == agent_pane)
            if agent_record['activity'] == 'working':
                break
            assert time.monotonic() < deadline, agent_record
            time.sleep(0.02)
        waited = subprocess.run([*panes_cli, 'wait', str(agent_pane), '--for', 'idle', '--timeout', '4'],
                                env=env, capture_output=True, text=True, timeout=5)
        assert waited.returncode == 0, waited.stderr
        idle_snapshot = subprocess.run([*panes_cli, '--json'], env=env,
                                       capture_output=True, text=True, timeout=5)
        idle_record = next(p for p in json.loads(idle_snapshot.stdout)['panes']
                           if p['pane_id'] == agent_pane)
        assert idle_record['activity'] == 'idle' and idle_record['coding_session']['last_turn_event'] == 'task_complete'
        request(rw, 'close', agent_pane)
        claude = private / 'claude'
        claude.write_text('#!/usr/bin/python3\n'
                          'import json,os,pathlib,time\n'
                          'pid=os.getpid()\n'
                          'raw=pathlib.Path(f"/proc/{pid}/stat").read_text()\n'
                          'ticks=raw[raw.rfind(")")+2:].split()[19]\n'
                          'path=pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"])/"sessions"/f"{pid}.json"\n'
                          'record={"pid":pid,"procStart":ticks,"sessionId":"abcdef12-1234-1234-1234-123456789abc","status":"working"}\n'
                          'for status in ("working", "idle"):\n'
                          '    record["status"]=status\n'
                          '    path.write_text(json.dumps(record))\n'
                          '    time.sleep(0.8)\n'
                          'time.sleep(3)\n')
        claude.chmod(0o700)
        claude_pane = request(rw, 'launch', first, b'\1' + str(claude).encode() + b'\0')['id']
        deadline = time.monotonic() + 5
        while True:
            claude_snapshot = subprocess.run([*panes_cli, '--json'], env=env,
                                             capture_output=True, text=True, timeout=5)
            assert claude_snapshot.returncode == 0, claude_snapshot.stderr
            claude_record = next(p for p in json.loads(claude_snapshot.stdout)['panes']
                                 if p['pane_id'] == claude_pane)
            if claude_record['activity'] == 'working':
                break
            assert time.monotonic() < deadline, claude_record
            time.sleep(0.02)
        waited_claude = subprocess.run([*panes_cli, 'wait', 'abcdef12', '--for', 'idle', '--timeout', '4'],
                                       env=env, capture_output=True, text=True, timeout=5)
        assert waited_claude.returncode == 0, waited_claude.stderr
        request(rw, 'close', claude_pane)
        listed = subprocess.run([*panes_cli, 'list'], env=env, capture_output=True, text=True, timeout=5)
        assert listed.returncode == 0 and 'PANE' in listed.stdout and str(launched) in listed.stdout
        focused = subprocess.run([*panes_cli, 'focus', str(first)],
                                 env=env, capture_output=True, text=True, timeout=5)
        assert focused.returncode == 0, focused.stderr
        assert next(p for p in request(rw, 'list')['panes'] if p['id'] == first)['active']
        persistent = subprocess.run([str(ROOT / 'kilix'), 'panes', '--socket', saved_endpoint, '--json'],
                                    env=env, capture_output=True, text=True, timeout=5)
        assert persistent.returncode == 0, persistent.stderr
        assert json.loads(persistent.stdout)['panes'][0]['batty_session']['name'] == 'saved-workspace'
        sent = subprocess.run([*panes_cli, 'send', str(launched), '--enter', 'PANE_CENTER_CLI'],
                              env=env, capture_output=True, text=True, timeout=5)
        assert sent.returncode == 0, sent.stderr
        deadline = time.monotonic() + 5
        while 'PANE_CENTER_CLI' not in request(rw, 'dump', launched):
            assert time.monotonic() < deadline
        dumped = subprocess.run([*panes_cli, 'dump', f'pane:{launched}', '--lines', '2'],
                                env=env, capture_output=True, text=True, timeout=5)
        assert dumped.returncode == 0 and 'PANE_CENTER_CLI' in dumped.stdout, dumped.stderr
        refused = subprocess.run([*panes_cli, 'send', str(launched), 'x' * 1025],
                                 env=env, capture_output=True, text=True, timeout=5)
        assert refused.returncode != 0 and '1024-byte' in refused.stderr
        assert 'rename' in request(rw, 'ping')['operations']
        assert 'rename' not in request(ro, 'ping')['operations']
        expect_error(lambda: request(ro, 'rename', observer, b'forbidden'), 'read-only')
        before_rename = events(rw)
        title = 'Work café "quoted" $(false)'
        request(rw, 'rename', first, title.encode())
        assert all(p['page_title'] == title for p in request(rw, 'list')['panes'])
        changed = events(rw, before_rename['cursor'], before_rename['epoch'])
        assert {e['pane'] for e in changed['events'] if e['changes'] & 2} == {first, launched}
        for invalid_title in (b'bad\0name', b'x' * 256):
            expect_error(lambda: request(rw, 'rename', first, invalid_title), 'Malformed')
        for invalid_title in (b'bad\xff', b'bad\x1bname', b'\xc0\xaf'):
            expect_error(lambda: request(rw, 'rename', first, invalid_title), 'printable UTF-8')
        assert request(rw, 'info', first)['panes'][0]['page_title'] == title
        renamed = subprocess.run([str(ROOT / 'battyctl'), '--socket', rw, 'rename', str(first), 'CLI name'],
                                 capture_output=True, text=True, timeout=5)
        assert renamed.returncode == 0, renamed.stderr
        assert request(rw, 'info', launched)['panes'][0]['page_title'] == 'CLI name'
        request(rw, 'rename', launched, b'')
        assert not request(rw, 'info', first)['panes'][0]['page_title']
        assert 'pane-rename' in request(rw, 'ping')['operations']
        assert 'pane-rename' not in request(ro, 'ping')['operations']
        expect_error(lambda: request(ro, 'pane-rename', observer, b'forbidden'), 'read-only')
        before_title = events(rw)
        request(rw, 'pane-rename', first, 'Agent λ'.encode())
        assert request(rw, 'info', first)['panes'][0]['title'] == 'Agent λ'
        title_events = events(rw, before_title['cursor'], before_title['epoch'])
        assert {e['pane'] for e in title_events['events'] if e['changes'] & 2} == {first}
        expect_error(lambda: request(rw, 'pane-rename', first, b'bad\0title'), 'Malformed')
        expect_error(lambda: request(rw, 'pane-rename', first, b'bad\xff'), 'printable UTF-8')
        request(rw, 'pane-rename', first, b'')
        request(rw, 'focus', launched)
        assert next(p for p in request(rw, 'list')['panes'] if p['id'] == launched)['active']
        request(rw, 'resize', launched, struct.pack('<Bi', 1, 500))
        request(rw, 'sync', launched, b'\1')
        request(rw, 'move', launched, b'\0')
        request(rw, 'zoom', launched)
        assert sum(p['visible'] for p in request(rw, 'list')['panes']) == 1
        request(rw, 'zoom', launched)
        request(rw, 'paste', launched, b'EXTERNAL_PASTE\n')
        deadline = time.monotonic() + 5
        while 'EXTERNAL_PASTE' not in request(rw, 'dump', launched):
            assert time.monotonic() < deadline
        assert 'EXTERNAL_PASTE' not in request(rw, 'dump', first)
        assert 'title' in request(rw, 'info', launched)['panes'][0]
        before = len(request(rw, 'list')['panes'])
        expect_error(lambda: request(rw, 'launch', 0, b'\1/nonexistent-batty-control-command\0'), '/nonexistent-batty-control-command')
        assert len(request(rw, 'list')['panes']) == before
        # Full argc/argv transfer preserves metacharacters without evaluation.
        literal = 'literal $(false); spaces'
        code = 'import os,sys;print(sys.argv[1], os.environ["BATTY_CONTROL"], flush=True);input()'
        args = [sys.executable, '-c', code, literal]
        child = request(rw, 'launch', 0, b'\1' + b'\0'.join(x.encode() for x in args) + b'\0')['id']
        deadline = time.monotonic() + 5
        while literal not in request(rw, 'dump', child):
            assert time.monotonic() < deadline
        assert rw in ''.join(request(rw, 'dump', child).splitlines())
        # Invalid versions, reserved fields, missing argv terminator, and size overflow.
        invalid = [b'BTC0' + bytes(12), b'BTC1\1\1' + bytes(10),
                   struct.pack('<4sB3xQ', b'BTC1', 10, 0) + b'\1unterminated', b'x' * 60001]
        for packet in invalid:
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
                connection.settimeout(5); connection.connect(rw); connection.sendall(packet)
                response = connection.recv(60000)
                assert response[:4] == b'BTC1' and struct.unpack_from('<I', response, 4)[0] == 1
        # Quiet clients expire rather than monopolizing the frontend indefinitely.
        stalled = []
        try:
            for _ in range(16):
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                connection.settimeout(5); connection.connect(rw); stalled.append(connection)
            assert request(rw, 'ping', timeout=5)['version'] == 1
        finally:
            for connection in stalled: connection.close()
        cli = subprocess.run([str(ROOT / 'battyctl'), '--socket', rw, 'list'], capture_output=True, text=True, timeout=5)
        assert cli.returncode == 0, cli.stderr
        assert len(json.loads(cli.stdout)['panes']) == 3
        # Kilix's own remote commands operate through Batty, including splits
        # originating from an unfocused pane instead of the active one.
        kilix_env = env | {'BATTY_CONTROL': rw}
        def kilix(*arguments):
            result = subprocess.run([str(ROOT / 'kilix'), *arguments], env=kilix_env,
                                    capture_output=True, text=True, timeout=5)
            assert result.returncode == 0, result.stderr
            return result.stdout
        unsupported_kitten = subprocess.run([str(ROOT / 'batty-kitten'), '@', 'get-text'],
                                            env=kilix_env, capture_output=True, text=True, timeout=5)
        assert unsupported_kitten.returncode == 2 and 'only @ launch' in unsupported_kitten.stderr
        state = json.loads(kilix('ls', '--json'))
        assert len(state['panes']) == 3 and all('cwd' in p for p in state['panes'])
        cwd = private / 'working directory with spaces'
        cwd.mkdir()
        origin_code = ('import os,sys,subprocess;os.chdir(sys.argv[1]);input();'
                       'p=subprocess.run([sys.argv[2],"new-pane","left","--","/bin/cat"],capture_output=True,text=True);'
                       'print("SOURCE_SPLIT="+p.stdout.strip()+p.stderr,flush=True);input()')
        origin_args = [sys.executable, '-c', origin_code, str(cwd), str(ROOT / 'kilix')]
        origin = request(rw, 'launch', 0, b'\1' + b'\0'.join(a.encode() for a in origin_args) + b'\0')['id']
        request(rw, 'focus', first)
        request(rw, 'send', origin, b'go\n')
        deadline = time.monotonic() + 5
        while True:
            text = request(rw, 'dump', origin)
            match = re.search(r'SOURCE_SPLIT=kilix new-pane: opened (\d+)', text)
            if match: break
            assert time.monotonic() < deadline, text
        split = int(match[1])
        state = {p['id']: p for p in json.loads(kilix('ls', '--json'))['panes']}
        assert state[split]['tab'] == state[origin]['tab'] != state[first]['tab']
        assert state[split]['x'] < state[origin]['x'] and state[split]['cwd'] == str(cwd)
        request(rw, 'focus', first)
        kilix('focus', f'tab:{state[split]["tab"]}')
        assert next(p for p in request(rw, 'list')['panes'] if p['id'] == split)['active']
        request(rw, 'send', split, b'KILIX_WATCH\n')
        deadline = time.monotonic() + 5
        while 'KILIX_WATCH' not in kilix('watch', '--once', '--plain', f'pane:{split}'):
            assert time.monotonic() < deadline
        page_command = [sys.executable, '-c',
                        'import os,time;print("HOST_ENV="+os.environ["KILIX_HOST_PROBE"],flush=True);time.sleep(20)']
        page_output = kilix('launch', '--type=tab', '--target', str(origin), '--cwd', str(cwd),
                            '--tab-title', 'App Page', '--pane-title', 'App Pane',
                            '--env', 'KILIX_HOST_PROBE=literal value', '--', *page_command)
        page = int(page_output.strip().rsplit(' ', 1)[1])
        page_info = request(rw, 'info', page)['panes'][0]
        assert page_info['tab'] != state[origin]['tab']
        assert page_info['page_title'] == 'App Page' and page_info['title'] == 'App Pane'
        deadline = time.monotonic() + 5
        while 'HOST_ENV=literal value' not in request(rw, 'dump', page):
            assert time.monotonic() < deadline, 'Child environment deadline'
        bad_env = subprocess.run([str(ROOT / 'kilix'), 'new-page', '--socket', rw,
                                  '--target', str(origin), '--env', 'BAD-NAME=value', '--', '/bin/true'],
                                 env=kilix_env, capture_output=True, text=True, timeout=5)
        assert bad_env.returncode != 0 and 'valid variable name' in bad_env.stderr
        for pane in (split, page, origin): request(rw, 'close', pane)
        # Named sessions keep their one state owner and child across view closure.
        persistent = request(rw, 'session', 0, b'\1\0control\0/bin/cat\0')['id']
        generated_looking = 'kilix-auto-0123456789abcdef01234567'
        generated_view = request(rw, 'session', 0,
                                 b'\1\0' + generated_looking.encode() + b'\0/bin/cat\0')['id']
        close_persistent(generated_view)
        generated_socket = Path(env['BATTY_TEST_SESSION_DIR']) / (generated_looking + '.sock')
        assert generated_socket.exists(), 'Generic Batty control unexpectedly applied Kilix close policy'
        stopped = subprocess.run([str(ROOT / 'batty'), '--terminate', generated_looking,
                                  '--session-dir', env['BATTY_TEST_SESSION_DIR']],
                                 env=env, capture_output=True, timeout=5)
        assert stopped.returncode == 0 and not generated_socket.exists(), stopped.stderr
        metadata = request(rw, 'info', persistent)['panes'][0]
        assert metadata['persistent'] and metadata['session'] == 'control' and not metadata['observe']
        assert metadata['session_dir'] == env['BATTY_TEST_SESSION_DIR']
        saved_epoch = metadata['session_epoch']
        assert len(saved_epoch) == 16 and int(saved_epoch, 16) > 0
        transport = metadata['frame_transport']
        assert transport['full_frames'] >= 1 and transport['full_bytes'] > 0
        assert all(isinstance(value, int) and value >= 0 for value in transport.values())
        original_pid = metadata['pid']
        assert original_pid > 0
        request(rw, 'send', persistent, b'PERSISTENT_CONTROL\n')
        deadline = time.monotonic() + 5
        while 'PERSISTENT_CONTROL' not in request(rw, 'dump', persistent):
            assert time.monotonic() < deadline
        watch = request(rw, 'session', 0, b'\1\2control\0')['id']
        assert request(rw, 'info', watch)['panes'][0]['observe']
        expect_error(lambda: request(rw, 'send', watch, b'forbidden'), 'Observer')
        expect_error(lambda: request(rw, 'paste', watch, b'forbidden'), 'Observer')
        close_persistent(persistent)
        attached = request(rw, 'session', 0, b'\1\1control\0')['id']
        assert attached != persistent
        assert request(rw, 'info', attached)['panes'][0]['session_epoch'] == saved_epoch
        assert request(rw, 'info', attached)['panes'][0]['pid'] == original_pid
        assert 'PERSISTENT_CONTROL' in request(rw, 'dump', attached)
        close_persistent(attached)
        request(rw, 'close', watch)
        cli = subprocess.run([str(ROOT / 'battyctl'), '--socket', rw, 'launch', '--session', 'control', '--attach'],
                             capture_output=True, text=True, timeout=5)
        assert cli.returncode == 0, cli.stderr
        attached = json.loads(cli.stdout)['id']
        assert request(rw, 'info', attached)['panes'][0]['pid'] == original_pid
        # A stopped owner times out only its own pane, while local control/input
        # and rendering continue. Derive identity from our private socket.
        request(rw, 'focus', first)
        before_failure = events(rw)
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as peer:
            peer.connect(str(Path(env['BATTY_TEST_SESSION_DIR']) / 'control.sock'))
            owner, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        assert owner > 0 and uid == os.getuid()
        stopped_owner = owner
        os.kill(owner, signal.SIGSTOP)
        deadline = time.monotonic() + 1
        while not re.search(r'^State:\s+T', Path(f'/proc/{owner}/status').read_text(), re.M):
            assert time.monotonic() < deadline
            time.sleep(0.002)
        started = time.monotonic()
        # Splitting and zooming the stopped pane must queue geometry rather
        # than turn an unrelated control request into a three-second wait.
        replacement = request(rw, 'launch', attached, b'\1/bin/cat\0')['id']
        request(rw, 'resize', replacement, struct.pack('<Bi', 1, 500))
        request(rw, 'zoom', attached)
        request(rw, 'zoom', attached)
        assert request(rw, 'info', attached)['panes'][0]['resize_pending']
        request(rw, 'send', attached, b'QUEUED_WHILE_STOPPED\n')
        pending_dump = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        pending_dump.settimeout(5)
        pending_dump.connect(rw)
        pending_dump.sendall(struct.pack('<4sB3xQ', b'BTC1', 6, attached))
        # Abandoned reads must not occupy frontend slots until the owner wakes.
        for _ in range(2):
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as abandoned:
                abandoned.connect(rw)
                abandoned.sendall(struct.pack('<4sB3xQ', b'BTC1', 6, attached))
                assert request(rw, 'ping')['version'] == 1
        request(rw, 'send', first, b'LIVE_DURING_OWNER_STOP\n')
        while 'LIVE_DURING_OWNER_STOP' not in request(rw, 'dump', first):
            assert time.monotonic() - started < 1
        assert time.monotonic() - started < 1
        deadline = time.monotonic() + 5
        while True:
            failed = request(rw, 'info', attached)['panes'][0]
            if failed['disconnected']:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        response = pending_dump.recv(60000)
        assert response[:4] == b'BTC1' and struct.unpack('<I', response[4:8])[0] == 1 and b'text' in response[8:], response
        pending_dump.close()
        pending_dump = None
        assert 'timed out' in failed['connection_error'], failed
        assert failed['pid'] == original_pid and failed['exit_status'] is None
        failure_reason = failed['connection_error']
        update = events(rw, before_failure['cursor'], before_failure['epoch'])
        assert any(e['pane'] == attached and e['changes'] & 8 for e in update['events']), update
        expect_error(lambda: request(rw, 'send', attached, b'rejected'), 'Could not deliver')
        expect_error(lambda: request(rw, 'paste', attached, b'rejected'), 'Could not deliver')
        assert request(rw, 'info', attached)['panes'][0]['connection_error'] == failure_reason
        assert not failed['resize_pending']
        request(rw, 'focus', attached)
        request(rw, 'resize', replacement, struct.pack('<Bi', 1, 500))
        request(rw, 'zoom', attached)
        request(rw, 'zoom', attached)
        request(rw, 'send', replacement, b'LIVE_AFTER_OWNER_FAILURE\n')
        deadline = time.monotonic() + 1
        while 'LIVE_AFTER_OWNER_FAILURE' not in request(rw, 'dump', replacement):
            assert time.monotonic() < deadline
        close_persistent(attached)
        request(rw, 'close', replacement)
        os.kill(owner, signal.SIGCONT)
        stopped_owner = 0
        attached = request(rw, 'session', 0, b'\1\1control\0')['id']
        recovered = request(rw, 'info', attached)['panes'][0]
        assert recovered['pid'] == original_pid and not recovered['disconnected'] and not recovered['connection_error']
        assert 'PERSISTENT_CONTROL' in request(rw, 'dump', attached)
        # Closing a view with queued input does not wait for the stopped owner.
        # The first pass resumes it; the second exercises bounded failure.
        for timeout_detach in (False, True):
            request(rw, 'focus', first)
            failed_before = request(rw, 'ping')['failed_detaches']
            stopped_owner = owner
            os.kill(owner, signal.SIGSTOP)
            deadline = time.monotonic() + 1
            while not re.search(r'^State:\s+T', Path(f'/proc/{owner}/status').read_text(), re.M):
                assert time.monotonic() < deadline
                time.sleep(0.002)
            request(rw, 'send', attached, b'DETACH_ACCEPTED\n')
            started_close = time.monotonic()
            request(rw, 'close', attached)
            assert time.monotonic() - started_close < 1, 'Pane close waited for its stopped owner'
            assert all(p['id'] != attached for p in request(rw, 'list')['panes'])
            assert request(rw, 'ping')['pending_detaches'] == 1
            request(rw, 'send', first, b'LIVE_DURING_DETACH\n')
            deadline = time.monotonic() + 1
            while 'LIVE_DURING_DETACH' not in request(rw, 'dump', first):
                assert time.monotonic() < deadline
            if timeout_detach:
                wait_detaches()
                assert request(rw, 'ping')['failed_detaches'] == failed_before + 1
            os.kill(owner, signal.SIGCONT)
            stopped_owner = 0
            wait_detaches()
            if not timeout_detach:
                assert request(rw, 'ping')['failed_detaches'] == failed_before
                attached = request(rw, 'session', 0, b'\1\1control\0')['id']
                assert request(rw, 'info', attached)['panes'][0]['pid'] == original_pid
                deadline = time.monotonic() + 5
                while 'DETACH_ACCEPTED' not in request(rw, 'dump', attached):
                    assert time.monotonic() < deadline
        expect_error(lambda: request(ro, 'session', 0, b'\1\2control\0'), 'read-only')
        expect_error(lambda: request(rw, 'session', 0, b'\1\1missing\0'), 'connect')
        expect_error(lambda: request(rw, 'session', 0, b'\1\1control\0extra\0'), 'Malformed')
        assert len(request(rw, 'list')['panes']) == 3
        # Event cursors recover from old histories and endpoint generations.
        initial = events(rw)
        assert initial['reset'] and len(initial['panes']) == 3 and not initial['events']
        assert events(ro)['reset']
        assert 'events' in request(ro, 'ping')['operations']
        assert events(rw, initial['cursor'] + 1000, initial['epoch'])['reset']
        assert events(rw, initial['cursor'], '0')['reset']
        expect_error(lambda: request(rw, 'events'), 'Malformed')
        # Drain asynchronous PTY changes, then hold a subscription while the
        # frontend continues accepting mutations and pumping real child output.
        latest = initial
        deadline = time.monotonic() + 5
        while True:
            latest = events(rw, latest['cursor'], latest['epoch'])
            assert time.monotonic() < deadline
            if not latest['events']: break
        assert not latest['reset']
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as pending:
            pending.settimeout(5); pending.connect(rw)
            pending.sendall(struct.pack('<4sB3xQQ', b'BTC1', 15, latest['cursor'], int(latest['epoch'], 16)))
            assert not select.select([pending], [], [], 0.05)[0]
            request(rw, 'send', first, b'EVENT_OUTPUT\n')
            response = pending.recv(60000)
            assert response[:8] == b'BTC1' + bytes(4)
            update = json.loads(response[8:])
            assert any(e['pane'] == first and e['changes'] & 4 for e in update['events']), update
        previous = events(rw)
        args = [sys.executable, '-c', 'import sys;sys.stdout.write("\\x1b]2;event-title\\x07");sys.stdout.flush()']
        event_pane = request(rw, 'launch', 0, b'\1' + b'\0'.join(a.encode() for a in args) + b'\0')['id']
        seen = []
        deadline = time.monotonic() + 5
        while not (any(e['kind'] == 'added' for e in seen) and
                   any(e['changes'] & 2 for e in seen) and any(e['changes'] & 8 for e in seen)):
            update = events(rw, previous['cursor'], previous['epoch'])
            seen.extend(e for e in update['events'] if e['pane'] == event_pane)
            previous = update
            assert time.monotonic() < deadline, seen
        assert request(rw, 'info', event_pane)['panes'][0]['title'] == 'event-title'
        request(rw, 'close', event_pane)
        update = events(rw, previous['cursor'], previous['epoch'])
        assert any(e['pane'] == event_pane and e['kind'] == 'removed' for e in update['events'])
        old = events(rw)
        for _ in range(130):
            request(rw, 'focus', first)
            request(rw, 'focus', launched)
        reset = events(rw, old['cursor'], old['epoch'])
        assert reset['reset'] and not reset['events'] and len(reset['panes']) == 3
        cli = subprocess.run([str(ROOT / 'battyctl'), '--socket', rw, 'events'],
                             capture_output=True, text=True, timeout=5)
        assert cli.returncode == 0, cli.stderr
        assert json.loads(cli.stdout)['reset']
        follower = subprocess.Popen([str(ROOT / 'battyctl'), '--socket', rw, 'events', '--follow'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert select.select([follower.stdout], [], [], 5)[0]
            assert json.loads(follower.stdout.readline())['reset']
            request(rw, 'focus', first)
            assert select.select([follower.stdout], [], [], 5)[0]
            batch = json.loads(follower.stdout.readline())
            assert any(e['pane'] == first and e['changes'] & 1 for e in batch['events']), batch
        finally:
            follower.terminate()
            follower.communicate(timeout=5)
        for pane in (launched, child): request(rw, 'close', pane)
        expect_error(lambda: request(rw, 'dump', launched), 'does not exist')
        request(rw, 'close', first)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, (stdout, stderr)
        assert not any((private / name).exists() for name in
                       ('rw.sock', 'ro.sock', 'scope-view.sock', 'scope-input.sock'))
    finally:
        if pending_dump is not None:
            pending_dump.close()
        if stopped_owner:
            os.kill(stopped_owner, signal.SIGCONT)
        if process.poll() is None:
            process.terminate()
            try: process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.communicate()
print('PASS external control: discovery, launch/input/layout, persistence, observers, event waits/recovery/follow, scopes, malformed clients and cleanup')
