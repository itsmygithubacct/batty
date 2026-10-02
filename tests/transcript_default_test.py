#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Default recording uses private XDG state; disabled and unsafe paths stay safe."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from control import request


def wait_for(check):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError('Default recording deadline')


with tempfile.TemporaryDirectory(prefix='bt-default-recording-') as directory:
    base = Path(directory)
    state = base / 'state with spaces'
    session_root = base / 'sessions'
    settings = base / 'settings.conf'; settings.write_text('KILIX_TRANSCRIPT=off\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith('BATTY_TRANSCRIPT_')}
    env.update(XDG_STATE_HOME=str(state), GPU_TERMINAL_SETTINGS_FILE=str(settings),
               BATTY_SESSION_DIR=str(session_root), BATTY_KILIX_AUTO_RECOVER='0',
               BATTY_KILIX_CONFIG=str(ROOT / 'tests/recording_config.bash'), BATTY_CONTROL_DIR=str(base / 'control'))
    processes = []
    def start(label, overrides=None):
        ready = base / (label + '.ready')
        log = (base / (label + '.out')).open('w')
        process = subprocess.Popen([str(ROOT / 'kilix'), '--', '/bin/cat'],
            env=env | {'BATTY_RECORDING_READY': str(ready)} | (overrides or {}), stdout=log, stderr=log)
        processes.append((process, log))
        endpoint = wait_for(lambda: ready.read_text().strip() if ready.exists() else None)
        return process, endpoint, request(endpoint, 'list')['panes'][0]
    def close(process, endpoint, pane):
        request(endpoint, 'close', pane['id']); process.wait(timeout=5)
    try:
        off, endpoint, pane = start('disabled')
        assert pane['recording'] == 'disabled' and not state.exists()
        close(off, endpoint, pane)
        # Missing shared toggle means on, with no explicit transcript directory.
        settings.write_text('')
        on, endpoint, pane = start('default')
        assert pane['recording'] == 'active'
        request(endpoint, 'send', pane['id'], b'DEFAULT_RECORDING_OK\n')
        logs = state / 'batty/transcripts'
        def recorded():
            files = list(logs.glob('*.log')) if logs.exists() else []
            return files[0] if files and b'DEFAULT_RECORDING_OK' in files[0].read_bytes() else None
        transcript = wait_for(recorded)
        for path in (state, state / 'batty', logs):
            assert path.stat().st_mode & 0o777 == 0o700
        assert transcript.stat().st_mode & 0o777 == 0o600
        child_env = Path(f'/proc/{pane["pid"]}/environ').read_bytes().split(b'\0')
        assert b'BATTY_TRANSCRIPT_LIMIT=8388608' in child_env
        assert b'BATTY_TRANSCRIPT_GRAPHICS=elide' in child_env
        assert ('BATTY_TRANSCRIPT_DIR=' + str(logs)).encode() in child_env
        def cli_path():
            result = subprocess.run([str(ROOT / 'kilix'), 'transcript', 'path'], env=env,
                                    capture_output=True, text=True, timeout=3)
            if result.returncode:
                assert 'maintenance is busy' in result.stderr, result.stderr
                return None
            return result.stdout.strip()
        assert wait_for(cli_path) == str(logs)
        close(on, endpoint, pane)
        unsafe_state = base / 'unsafe-state'
        unsafe_logs = unsafe_state / 'batty/transcripts'; unsafe_logs.mkdir(parents=True)
        unsafe_logs.chmod(0o755)
        sentinel = unsafe_logs / 'sentinel'; sentinel.write_text('preserve')
        failed, endpoint, pane = start('unsafe', {'XDG_STATE_HOME': str(unsafe_state)})
        wait_for(lambda: request(endpoint, 'list')['panes'][0]['recording'] == 'failed')
        request(endpoint, 'send', pane['id'], b'UNSAFE_STORAGE_TERMINAL_OK\n')
        wait_for(lambda: 'UNSAFE_STORAGE_TERMINAL_OK' in request(endpoint, 'dump', pane['id']))
        assert unsafe_logs.stat().st_mode & 0o777 == 0o755 and sentinel.read_text() == 'preserve'
        assert sorted(p.name for p in unsafe_logs.iterdir()) == ['sentinel']
        close(failed, endpoint, pane)
    finally:
        for process, log in processes:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
            log.close()
        for socket in session_root.glob('kilix-auto-*.sock'):
            subprocess.run([str(ROOT / 'batty'), '--terminate', socket.stem,
                            '--session-dir', str(session_root)], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
print('PASS default recording: private XDG setup, shared off, default policy, CLI lookup and isolated storage failure')
