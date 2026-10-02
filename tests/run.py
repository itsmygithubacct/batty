#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise the native session, Bash loadable and a real window on an isolated X server."""
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import tempfile
import time

root = Path(__file__).resolve().parent.parent
os.chdir(root)
env = {k: v for k, v in os.environ.items() if k not in (
    'BASH_ENV', 'ENV', 'SHELLOPTS', 'BASHOPTS', 'BATTY_TRANSCRIPT_DIR',
    'BATTY_TRANSCRIPT_ENABLED', 'BATTY_TRANSCRIPT_LIMIT', 'BATTY_TRANSCRIPT_GRAPHICS',
    'BATTY_TRANSCRIPT_MAINTENANCE', 'BATTY_TRANSCRIPT_CREATE_ROOT')}
env.update(LC_ALL='C.UTF-8')
runtime = json.loads((root / 'build/bash-os.json').read_text())
bash = Path(runtime['binary'])
if not bash.is_file() or hashlib.sha256(bash.read_bytes()).hexdigest() != runtime['sha256']:
    raise SystemExit('Run ./build.sh to prepare the verified bash-os runtime.')
env['BATTY_BASH'] = str(bash)
# ./test.sh has just checked the remote. All tests use that one verified build.
env['BATTY_OFFLINE'] = '1'
(root / 'build/test-shared-settings.conf').write_text('KILIX_CHROME_TAB_BAR_EDGE=top\nKILIX_TRANSCRIPT=off\n')
test_state = tempfile.TemporaryDirectory(prefix='bt-suite-state-')
env['XDG_STATE_HOME'] = test_state.name
env['GPU_TERMINAL_SETTINGS_FILE'] = str(root / 'build/test-shared-settings.conf')
env['BATTY_TAB_BAR_EDGE'] = ''
results = []

def run(name, argv, expected=0, overrides=None, timeout=35):
    started = time.monotonic()
    try:
        p = subprocess.run(argv, env=env | (overrides or {}), capture_output=True, text=True, timeout=timeout)
        result = dict(name=name, code=p.returncode, expected=expected,
                      seconds=round(time.monotonic() - started, 3), stdout=p.stdout, stderr=p.stderr,
                      passed=p.returncode == expected)
    except subprocess.TimeoutExpired as exc:
        result = dict(name=name, passed=False, error=f'{timeout} second deadline exceeded',
                      stdout=str(exc.stdout), stderr=str(exc.stderr))
    results.append(result)
    print(name + ': ' + ('PASS' if result['passed'] else 'FAIL'), flush=True)
    print(result.get('stdout', '') + result.get('stderr', ''), end='', flush=True)
    return result['passed']

def run_persistent(name, argv, gpu_env, names, expected=0):
    # Keep the root outside the test process so a test deadline/SIGKILL cannot
    # strand a deliberately persistent service. Never touch user session roots.
    private = Path(tempfile.mkdtemp(prefix='bt-test-'))
    passed = False
    try:
        passed = run(name, argv, expected=expected,
                     overrides=gpu_env | {'BATTY_TEST_SESSION_DIR': str(private),
                                          'BATTY_SESSION_DIR': str(private)})
    finally:
        failures = []
        for session in set(names) | {path.stem for path in private.glob('kilix-auto-*.sock')}:
            endpoint = private / (session + '.sock')
            if not endpoint.exists() or endpoint.is_symlink():
                continue
            try:
                cleanup = subprocess.run(['./batty', '--terminate', session, '--session-dir', str(private)],
                                         env=env | gpu_env | {'BATTY_CONFIG': '/dev/null'},
                                         capture_output=True, text=True, timeout=10)
                if endpoint.exists():
                    failures.append(dict(session=session, code=cleanup.returncode, stderr=cleanup.stderr))
            except subprocess.TimeoutExpired:
                failures.append(dict(session=session, error='cleanup deadline exceeded'))
        if failures:
            results.append(dict(name=name + '-cleanup', passed=False, root=str(private), failures=failures))
            print(name + '-cleanup: FAIL; private root retained at ' + str(private), flush=True)
            passed = False
        elif private.exists():
            shutil.rmtree(private)
    return passed

run('bash-os-runtime-manager', ['python3', 'tests/runtime_test.py'])
run('shared-settings', ['python3', 'tests/settings_test.py'])
run('application-catalog', ['python3', 'tests/catalog_test.py'])
run('pinned-content-applications', ['python3', '-B', 'tests/content_app_test.py'])
run('pinned-content-install-list', ['python3', '-B', 'tests/install_test.py'])
run('default-desktop-choice', ['python3', '-B', 'tests/default_desktop_test.py'])
run('kilix-status', ['python3', '-B', 'tests/status_test.py'])
run('kilix-tui-launch', ['python3', '-B', 'tests/tui_launch_test.py'])
run('kilix-screensaver', ['python3', '-B', 'tests/screensaver_test.py'])
run('kilix-games', ['python3', '-B', 'tests/games_test.py'])
run('agent-turn-telemetry', ['python3', 'tests/agent_test.py'])
run('provider-network-vnc', ['python3', 'tests/provider_network_test.py'])
run('provider-network-audio', ['python3', 'tests/provider_audio_network_test.py'], timeout=120)
run('provider-network-hls-tls', ['python3', 'tests/provider_hls_network_test.py'], timeout=120)
run('kilix-share', ['python3', '-B', 'tests/share_test.py'], timeout=75)
run('provider-real-xterm', ['python3', 'tests/provider_xterm_test.py'])
run('desktop-integration', ['python3', 'tests/desktop_test.py'])
run('native-session', ['./build/session-test'])
run('sixel-decoder', ['./build/sixel-test'])
run('pane-layout', ['./build/layout-test'])
run('transcript-filter', ['./build/transcript-filter-test'])
run('transcript-worker', ['python3', 'tests/transcript_worker_test.py'])
run('transcript-index', ['python3', 'tests/transcript_index_test.py'])
run('transcript-retention', ['python3', 'tests/transcript_retention_test.py'])
run('transcript-maintenance', ['python3', 'tests/transcript_maintenance_test.py'])
if run('bash-builtin', [str(bash), '--noprofile', '--norc', 'tests/builtin.bash']):
    text = (root / 'build/builtin-screen.txt').read_text()
    results.append(dict(name='exported-environment', passed='ONE:exported_after_shell_start' in text))

if not shutil.which('Xvfb'):
    raise SystemExit('Xvfb is required for the window integration checks.')
read_fd, write_fd = os.pipe()
xlog = (root / 'build/xvfb.log').open('w')
# Keep the server alive between the final window closing and the next SDL
# initialization. An automatic X server reset can reject that next connection.
xserver = subprocess.Popen(['Xvfb', '-displayfd', str(write_fd), '-screen', '0', '1600x1000x24', '-noreset', '-nolisten', 'tcp'],
                           pass_fds=(write_fd,), stdout=xlog, stderr=xlog)
os.close(write_fd)
try:
    if not select.select([read_fd], [], [], 10)[0]:
        raise RuntimeError('Xvfb startup deadline exceeded')
    # Xvfb can write the number and newline separately. Closing after a short
    # read can break its second write and make an otherwise ready server exit.
    with os.fdopen(read_fd, 'rb') as display_pipe:
        display_line = display_pipe.readline(128)
    display = display_line.decode().strip()
    if not display_line.endswith(b'\n') or not display.isdigit():
        raise RuntimeError('Xvfb did not supply a display number')
    gpu_env = {'DISPLAY': ':' + display, 'SDL_VIDEODRIVER': 'x11', 'LIBGL_ALWAYS_SOFTWARE': '1'}
    run_persistent('semantic-frame-source', ['python3', 'tests/frame_source_test.py'], gpu_env, ('frame',))
    run_persistent('semantic-input-source', ['python3', 'tests/semantic_input_source_test.py'], gpu_env, ('input',))
    run_persistent('semantic-stream-server', ['python3', 'tests/semantic_stream_server_test.py'], gpu_env, ('stream',))
    run_persistent('semantic-stream-graphics', ['python3', 'tests/semantic_stream_graphics_test.py'], gpu_env, ('graphics-stream',))
    run_persistent('semantic-stream-mirror', ['python3', 'tests/semantic_stream_mirror_test.py'], gpu_env, ('source',))
    run_persistent('semantic-stream-group', ['python3', 'tests/semantic_stream_group_test.py'], gpu_env,
                   ('group-one', 'group-two'))
    run_persistent('semantic-stream-serve-group', ['python3', 'tests/semantic_stream_serve_group_test.py'],
                   gpu_env, ('serve-one', 'serve-two'))
    run_persistent('semantic-stream-reconnect', ['python3', 'tests/semantic_stream_reconnect_test.py'], gpu_env, ('reconnect',))
    run_persistent('semantic-stream-retarget', ['python3', 'tests/semantic_stream_retarget_test.py'], gpu_env, ('retarget', 'other-owner'))
    run_persistent('semantic-stream-audio', ['python3', 'tests/semantic_stream_audio_test.py'], gpu_env, ('audio',))
    run_persistent('semantic-stream-input', ['python3', 'tests/semantic_stream_input_test.py'], gpu_env, ('interactive',))
    run_persistent('semantic-stream-clipboard', ['python3', 'tests/semantic_stream_clipboard_test.py'], gpu_env, ('clipboard',))
    run_persistent('semantic-stream-cli-input', ['python3', 'tests/semantic_stream_cli_input_test.py'], gpu_env, ('cli-input',))
    run_persistent('terminal-views', ['./build/views-test'], gpu_env, ('view',))
    run('native-workspace', ['./build/workspace-test'], overrides=gpu_env)
    run_persistent('workspace-control', ['python3', 'tests/control_test.py'], gpu_env, ('control',))
    run('kilix-ephemeral-app', ['python3', '-B', 'tests/ephemeral_app_test.py'], overrides=gpu_env)
    run('kilix-laptop', ['python3', '-B', 'tests/laptop_test.py'], overrides=gpu_env)
    run_persistent('kilix-os-window-launch', ['python3', 'tests/window_launch_test.py'], gpu_env, ())
    run_persistent('recording-settings', ['python3', 'tests/recording_settings_test.py'], gpu_env, ('recording-settings',))
    run('automatic-transcript-maintenance', ['python3', 'tests/transcript_automatic_test.py'], overrides=gpu_env)
    run('default-transcript-recording', ['python3', 'tests/transcript_default_test.py'], overrides=gpu_env)
    run_persistent('kilix-workspace-restore', ['python3', 'tests/restore_test.py'], gpu_env, ('restore-first', 'restore-second', 'restore-third'))
    run('kilix-automatic-recovery', ['python3', 'tests/automatic_recovery_test.py'], overrides=gpu_env)
    run('kilix-durable-recovery', ['python3', 'tests/durable_recovery_test.py'], overrides=gpu_env, timeout=45)
    run('kilix-close-confirmation', ['python3', 'tests/close_confirmation_test.py'], overrides=gpu_env)
    run_persistent('persistent-control-discovery', ['python3', 'tests/discovery_test.py'], gpu_env, ('discovery',))
    run('kilix-application-provider', ['python3', 'tests/provider_test.py'], overrides=gpu_env, timeout=60)
    run_persistent('kilix-controller', ['./kilix', '--', str(bash), '--noprofile', '--norc', '-c',
                                        'read -r line; test "$line" = ready || exit 2; exit 9'],
                   gpu_env | {'BATTY_KILIX_CONFIG': str(root / 'tests/kilix_config.bash'),
                              'BATTY_SHELL': '/bin/cat', 'KITTY_KILIX_RENDERING': '0'}, (), expected=9)
    run_persistent('bash-workspace', [str(bash), '--noprofile', '--norc', 'tests/workspace.bash'],
                   gpu_env, ('workspace',))
    if run_persistent('persistent-session', ['./build/persistence-test'], gpu_env, ('main', 'tree', 'orphan', 'cancel', 'badexec', 'record')):
        try:
            from PIL import Image
        except ImportError:
            pass
        else:
            for name in ('persistence-first', 'persistence-second'):
                with Image.open(root / ('build/' + name + '.ppm')) as frame:
                    frame.save(root / ('build/' + name + '.png'))
    run_persistent('persistent-bash-cli', [str(bash), '--noprofile', '--norc', 'tests/persistence.bash'],
                   gpu_env, ('shell', 'hard', 'cli', 'badexec'))
    if run('native-graphics', ['./build/graphics-test'], overrides=gpu_env):
        try:
            from PIL import Image
        except ImportError:
            pass
        else:
            with Image.open(root / 'build/graphics-test.ppm') as frame:
                frame.save(root / 'build/graphics-test.png')
    run('cursor-rendering', ['./build/cursor-test'], overrides=gpu_env)
    if run('window-integration', ['./build/window-test'], overrides=gpu_env):
        capture = root / 'build/window-test.ppm'
        with capture.open('rb') as frame:
            magic = frame.readline()
            width, height = map(int, frame.readline().split())
            maximum = frame.readline()
            pixels = frame.read()
        colors = len(set(zip(pixels[0::3], pixels[1::3], pixels[2::3])))
        results.append(dict(name='rendered-framebuffer', width=width, height=height, colors=colors,
                            passed=magic == b'P6\n' and maximum == b'255\n'
                            and len(pixels) == width * height * 3 and colors > 32))
        try:
            from PIL import Image
        except ImportError:
            pass
        else:
            with Image.open(capture) as frame:
                frame.save(root / 'build/window-test.png')
    for name, flag in [('vim', '--editor'), ('less', '--pager')]:
        program = shutil.which(name)
        if program:
            run(name + '-window', ['./build/window-test', flag, program], overrides=gpu_env)
        else:
            results.append(dict(name=name + '-window', passed=True, skipped='program not installed'))
    run('bash-desktop-launcher', ['./batty', '--', str(bash), '--noprofile', '--norc', '-c',
                                 'printf "Batty desktop launcher\\n"; exit 7'], expected=7, overrides=gpu_env)
    with tempfile.TemporaryDirectory(prefix='batty-default-shell-') as directory:
        home = Path(directory)
        # Observe both the controller and the actual default interactive child.
        probe = '''printf '%s\\n' "$BASH" "$SHELL" "$BATTY_BASH_OS_REVISION" "$(type -t ls)" "$(type -t pty)" "$(type -t gpu)"'''
        config = home / 'config.bash'
        config.write_text(probe + ' > "$HOME/controller-result"\n')
        (home / '.bashrc').write_text(probe + ' > "$HOME/child-result"\nexit 19\n')
        overrides = gpu_env | dict(HOME=str(home), BATTY_CONFIG=str(config), BATTY_SHELL='',
                                   BATTY_BASH='/bin/bash')
        if run('bash-os-default-shell', ['./batty'], expected=19, overrides=overrides):
            expected = [str(bash), str(bash), runtime['revision'], 'builtin', 'builtin', 'builtin']
            observed = {name: (home / (name + '-result')).read_text().splitlines()
                        for name in ('controller', 'child')}
            results.append(dict(name='bash-os-controller-and-child', observed=observed,
                                passed=all(lines == expected for lines in observed.values())))
    if run('visual-demo', ['./visual-test', '--seconds', '1', '--fps', '12'], overrides=gpu_env):
        info = results[-1]['stdout']
        results.append(dict(name='graphics-context-report', passed=all(key in info for key in
                            ('GL_VENDOR:', 'GL_RENDERER:', 'GL_VERSION: OpenGL ES', 'GLSL:'))))
    if (root / 'build/window-test.png').exists():
        try:
            import PIL
        except ImportError:
            results.append(dict(name='image-viewer', passed=True, skipped='Pillow not installed'))
        else:
            run('image-viewer', ['./visual-test', '--image', 'build/window-test.png',
                                 '--seconds', '1', '--fps', '2'], overrides=gpu_env)
            run('native-image-viewer', ['./graphics-test', '--image', 'build/window-test.png',
                                        '--protocol', 'kitty', '--seconds', '1'], overrides=gpu_env)
    run('graphics-protocol-demo', ['./graphics-test', '--seconds', '1'], overrides=gpu_env)
    run('graphics-demo-framebuffer', [str(bash), '--noprofile', '--norc', 'tests/graphics-demo.bash'], overrides=gpu_env)
finally:
    xserver.terminate()
    try:
        xserver.wait(timeout=5)
    except subprocess.TimeoutExpired:
        xserver.kill()
        xserver.wait()
    xlog.close()

result = dict(bash_sha256=hashlib.sha256(bash.read_bytes()).hexdigest(),
              bash_os_revision=runtime['revision'], bash_os_binary=str(bash),
              ghostty_revision=(root / 'build/ghostty/revision').read_text().strip(),
              ghostty_patches_sha256=(root / 'build/ghostty/patches.sha256').read_text().strip(),
              graphics='Xvfb with Mesa software rendering; GLES path exercised, no hardware benchmark',
              results=results, passed=all(r['passed'] for r in results))
(root / 'build/test-results.json').write_text(json.dumps(result, indent=2) + '\n')
raise SystemExit(0 if result['passed'] else 1)
