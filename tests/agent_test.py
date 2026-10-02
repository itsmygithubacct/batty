#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Real /proc ownership and explicit Codex turn-boundary behavior."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
from kilix_agent import coding_session


with tempfile.TemporaryDirectory(prefix='bt-agent-') as directory:
    path = Path(directory) / 'rollout-2026-09-23-12345678-1234-1234-1234-123456789abc.jsonl'
    path.write_text('')
    with path.open('ab') as stream:
        child = subprocess.Popen(['codex', '30'], executable='/bin/sleep', pass_fds=(stream.fileno(),))
        record = {'pid': child.pid, 'name': 'sleep', 'argv': ['codex', '30'], 'cwd': directory}

        def append(event):
            stream.write((json.dumps({'type': 'event_msg', 'payload': {'type': event}}) + '\n').encode())
            stream.flush()

        def message(kind, value):
            stream.write((json.dumps({'type': 'event_msg',
                                      'payload': {'type': kind, 'message': value}}) + '\n').encode())
            stream.flush()

        try:
            session, activity = coding_session([record], directory)
            assert activity == 'agent' and session['live_status'] == 'unknown', session
            message('user_message', 'Build the Pane Center task display')
            append('task_started')
            session, activity = coding_session([record], directory)
            assert activity == 'working' and session['last_turn_event'] == 'task_started', session
            assert session['last_user_message'] == 'Build the Pane Center task display', session
            append('task_complete')
            session, activity = coding_session([record], directory)
            assert activity == 'idle' and session['session_id'] == '12345678-1234-1234-1234-123456789abc', session
            assert session['live_pids'] == [child.pid] and session['path'] == str(path), session
            message('user_message', '<codex_internal_context source="goal">keep going</codex_internal_context>')
            session, activity = coding_session([record], directory)
            assert session['last_user_message'] == 'Build the Pane Center task display', session
            append('task_started')
            session, activity = coding_session([record], directory)
            assert activity == 'working' and not session['last_user_message'], session
            stream.write((json.dumps({'type': 'response_item',
                                      'payload': {'type': 'message', 'role': 'user',
                                                  'content': [{'type': 'input_text',
                                                               'text': 'Show current work'}]}}) + '\n').encode())
            stream.flush()
            session, activity = coding_session([record], directory)
            assert session['last_user_message'] == 'Show current work', session
            stream.write(b'{"type":"event_msg","payload":{"type":"task_started"}')
            stream.flush()
            session, activity = coding_session([record], directory)
            assert activity == 'agent' and session['live_status'] == 'unknown' and not session['last_user_message'], session
            stream.write(b'}\n')
            stream.flush()
            session, activity = coding_session([record], directory)
            assert activity == 'working', session
        finally:
            child.terminate()
            child.wait(timeout=5)
    session, activity = coding_session([record], directory)
    assert activity == 'agent' and session['path'] is None, session

    config = Path(directory) / 'claude-config'
    (config / 'sessions').mkdir(parents=True)
    old_config = os.environ.get('CLAUDE_CONFIG_DIR')
    os.environ['CLAUDE_CONFIG_DIR'] = str(config)
    claude = subprocess.Popen(['claude', '30'], executable='/bin/sleep')
    claude_record = {'pid': claude.pid, 'name': 'sleep', 'argv': ['claude', '30'], 'cwd': directory}
    try:
        raw = (Path('/proc') / str(claude.pid) / 'stat').read_text()
        ticks = raw[raw.rfind(')') + 2:].split()[19]
        descriptor = config / 'sessions' / f'{claude.pid}.json'
        metadata = {'pid': claude.pid, 'procStart': ticks,
                    'sessionId': 'abcdef12-1234-1234-1234-123456789abc',
                    'status': 'working', 'cwd': directory, 'name': 'test task'}
        descriptor.write_text(json.dumps(metadata))
        session, activity = coding_session([claude_record], directory)
        assert activity == 'working' and session['title'] == 'test task', session
        metadata['status'] = 'idle'
        descriptor.write_text(json.dumps(metadata))
        session, activity = coding_session([claude_record], directory)
        assert activity == 'idle' and session['session_id'] == metadata['sessionId'], session
        metadata['procStart'] = str(int(ticks) + 1)
        descriptor.write_text(json.dumps(metadata))
        session, activity = coding_session([claude_record], directory)
        assert activity == 'agent' and session['session_id'] == 'unknown', session
    finally:
        claude.terminate()
        claude.wait(timeout=5)
        if old_config is None:
            del os.environ['CLAUDE_CONFIG_DIR']
        else:
            os.environ['CLAUDE_CONFIG_DIR'] = old_config

print('PASS Codex file owner and Claude PID/start-time registry turn states')
