#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise the pinned application host contract without network installs."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import kilix_content_app as app
import kilix_voice as voice
import kilix_desktop_launch as desktop_launch
import kilix_nvr_build as nvr_build


with tempfile.TemporaryDirectory(prefix='bt-nvr-command-') as directory:
    stub = Path(directory) / 'python3'
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$BATTY_CLI_CAPTURE"\n')
    stub.chmod(0o700)
    capture = Path(directory) / 'argv'
    environment = os.environ | {'PATH': directory + os.pathsep + os.environ.get('PATH', ''),
                                'BATTY_CLI_CAPTURE': str(capture)}
    for argument in ('nvr', 'record', 'recorder'):
        subprocess.run([str(ROOT / 'kilix'), argument, 'cameras'], env=environment,
                       check=True, capture_output=True, text=True)
        assert capture.read_text().splitlines() == [
            '-B', str(ROOT / 'tools/kilix_content_app.py'), 'run', 'kilix-nvr', '--', 'cameras']
    subprocess.run([str(ROOT / 'kilix'), 'nvr', '--install-only'], env=environment,
                   check=True, capture_output=True, text=True)
    assert capture.read_text().splitlines() == [
        '-B', str(ROOT / 'tools/kilix_content_app.py'), 'install', 'kilix-nvr']
    capture.unlink()
    help_result = subprocess.run([str(ROOT / 'kilix'), 'nvr', '--help'], env=environment,
                                 check=True, capture_output=True, text=True)
    assert 'kilix nvr' in help_result.stdout and not capture.exists()
    subprocess.run([str(ROOT / 'kilix'), 'nvr', '--force-install'], env=environment,
                   check=True, capture_output=True, text=True)
    assert capture.read_text().splitlines() == [
        '-B', str(ROOT / 'tools/kilix_content_app.py'), 'reinstall', 'kilix-nvr']
    for argument in ('rtsp', 'cameras', 'camera'):
        subprocess.run([str(ROOT / 'kilix'), argument, 'list'], env=environment,
                       check=True, capture_output=True, text=True)
        assert capture.read_text().splitlines() == [
            '-B', str(ROOT / 'tools/kilix_content_app.py'), 'run', 'kilix-rtsp', '--', 'list']
    subprocess.run([str(ROOT / 'kilix'), 'rtsp', '--install-only'], env=environment,
                   check=True, capture_output=True, text=True)
    assert capture.read_text().splitlines() == [
        '-B', str(ROOT / 'tools/kilix_content_app.py'), 'install', 'kilix-rtsp']
    subprocess.run([str(ROOT / 'kilix'), 'rtsp', '--force-install'], env=environment,
                   check=True, capture_output=True, text=True)
    assert capture.read_text().splitlines() == [
        '-B', str(ROOT / 'tools/kilix_content_app.py'), 'reinstall', 'kilix-rtsp']
    capture.unlink()
    help_result = subprocess.run([str(ROOT / 'kilix'), 'rtsp', '--help'], env=environment,
                                 check=True, capture_output=True, text=True)
    assert 'kilix rtsp' in help_result.stdout and not capture.exists()
    for argument in ('look', 'analyze', 'analyse'):
        subprocess.run([str(ROOT / 'kilix'), argument, 'classes'], env=environment,
                       check=True, capture_output=True, text=True)
        assert capture.read_text().splitlines() == [
            '-B', str(ROOT / 'tools/kilix_content_app.py'), 'run',
            'kilix-object-detect', '--', 'classes']
    for option, verb in (('--install-only', 'install'), ('--force-install', 'reinstall')):
        subprocess.run([str(ROOT / 'kilix'), 'look', option], env=environment,
                       check=True, capture_output=True, text=True)
        assert capture.read_text().splitlines() == [
            '-B', str(ROOT / 'tools/kilix_content_app.py'), verb, 'kilix-object-detect']
    capture.unlink()
    help_result = subprocess.run([str(ROOT / 'kilix'), 'look', '--help'], env=environment,
                                 check=True, capture_output=True, text=True)
    assert 'kilix look' in help_result.stdout and not capture.exists()
    for argument in ('pdf-view', 'pdf-viewer'):
        subprocess.run([str(ROOT / 'kilix'), argument, 'sample.pdf'], env=environment,
                       check=True, capture_output=True, text=True)
        assert capture.read_text().splitlines() == [
            '-B', str(ROOT / 'tools/kilix_content_app.py'), 'run', 'kilix-pdf', '--', 'sample.pdf']
    for option, verb in (('--install-only', 'install'), ('--print-ref', 'ref')):
        subprocess.run([str(ROOT / 'kilix'), 'pdf-view', option], env=environment,
                       check=True, capture_output=True, text=True)
        assert capture.read_text().splitlines() == [
            '-B', str(ROOT / 'tools/kilix_content_app.py'), verb, 'kilix-pdf']
    capture.unlink()
    help_result = subprocess.run([str(ROOT / 'kilix'), 'pdf-view', '--help'], env=environment,
                                 check=True, capture_output=True, text=True)
    assert 'kilix pdf-view' in help_result.stdout and not capture.exists()


with tempfile.TemporaryDirectory(prefix='bt-catalog-reinstall-') as directory:
    origin = Path(directory) / 'origin'
    origin.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=origin, check=True)
    runner = origin / 'runner'
    runner.write_text('#!/bin/sh\nprintf "ready\\n"\n')
    runner.chmod(0o700)
    subprocess.run(['git', 'add', 'runner'], cwd=origin, check=True)
    subprocess.run(['git', '-c', 'user.name=Fixture',
                    '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'],
                   cwd=origin, check=True)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=origin,
                                       text=True).strip()
    spec = replace(app.application_spec('kilix-nvr'), repository=str(origin),
                   ref=revision, binary='runner', build=())
    installer = app.Installer(str(Path(directory) / 'managed'),
                              env=os.environ | {'GIT_ALLOW_PROTOCOL': 'file'})
    selected = Path(installer.ensure(spec))
    assert selected.read_text() == runner.read_text()
    assert Path(installer.reinstall(spec)) == selected
    assert selected.read_text() == runner.read_text()
    failing = replace(spec, build=('sh', '-c', 'exit 7'))
    try:
        installer.reinstall(failing)
    except app.InstallError:
        pass
    else:
        raise AssertionError('failed rebuild replaced the selected NVR source')
    assert selected.read_text() == runner.read_text()
    selected.write_text('#!/bin/sh\nmodified\n')
    try:
        installer.reinstall(spec)
    except app.InstallError:
        pass
    else:
        raise AssertionError('modified managed checkout was replaced')
    assert 'modified' in selected.read_text()


with tempfile.TemporaryDirectory(prefix='bt-content-app-') as temporary:
    root = Path(temporary)
    root.chmod(0o700)
    with patch.dict(os.environ, {'BATTY_KILIX_STORAGE_HOME': str(root),
                                 'KILIX_RC_PASSWORD_FILE': '/tmp/foreign-password'}):
        voice_env = voice.environment()
        assert voice.spec('stt').ref == voice.REF
        assert voice_env['KILIX_HOME'] == str(ROOT)
        assert voice_env['KILIX_DATA_HOME'] == str(root / 'data')
        assert voice_env['KILIX_VOICE_PREFIX'] == str(root / 'data/voice/prefix')
        assert voice_env['GPU_TERMINAL_SOURCE_HOME'] == str(root / 'data/sources')
        assert 'KILIX_RC_PASSWORD_FILE' not in voice_env
        refs = subprocess.run([str(ROOT / 'kilix'), 'voice', 'install', '--print-refs'],
                              env=voice_env, capture_output=True, text=True, timeout=5)
        assert refs.returncode == 0 and f'kilix-voice={voice.REF}' in refs.stdout
        missing = subprocess.run([str(ROOT / 'kilix'), 'voice', 'status'],
                                 env=voice_env, capture_output=True, text=True, timeout=5)
        assert missing.returncode == 0 and 'not installed' in missing.stdout
        stopped = subprocess.run([str(ROOT / 'kilix'), 'voice', 'stop'],
                                 env=voice_env, capture_output=True, text=True, timeout=5)
        assert stopped.returncode == 0 and 'not running' in stopped.stdout
    with patch.object(voice, 'ensure_daemon') as start_voice, \
            patch.object(voice, 'voice_request') as speech, \
            patch.object(voice, 'active_pane_text', return_value='visible pane'):
        assert voice.main(['speak']) == 0
        speech.assert_called_once_with('speak', text='visible pane')
        assert start_voice.call_count == 1
        speech.reset_mock()
        assert voice.main(['speak', 'literal', 'text']) == 0
        speech.assert_called_once_with('speak', text='literal text')
    with patch.object(voice, 'ensure_daemon'), \
            patch.object(voice, 'voice_request') as speech, \
            patch.object(voice, 'active_pane', return_value=('private-control', {'id': 7})) as chosen, \
            patch.object(voice, 'pane_text', return_value='target text'), \
            patch.object(voice, 'notice'):
        assert voice.main(['speak', '--socket', 'private-control', '--pane', '7']) == 0
        chosen.assert_called_once_with('private-control', '7')
        speech.assert_called_once_with('speak', text='target text')
    with patch.object(voice, 'resolve_endpoint', return_value='private-control'), \
            patch.object(voice, 'ancestors', return_value=iter((22,))), \
            patch.object(voice, 'request', side_effect=[
                {'panes': [{'id': 7, 'pid': 22, 'exit_status': None,
                            'observe': False, 'active': True}]}, '  pane output  ']):
        assert voice.active_pane_text() == 'pane output'
    with patch.object(voice, 'resolve_endpoint', return_value='private-control') as endpoint, \
            patch.object(voice, 'request', side_effect=[
                {'panes': [{'id': 7, 'pid': 22, 'exit_status': None,
                            'observe': False, 'active': True}]}, '  selected pane  ']):
        selected = voice.active_pane('private-control', '7')
        assert voice.pane_text(*selected) == 'selected pane'
        endpoint.assert_called_once_with('private-control')
    def fake_voice_request(operation, **fields):
        if operation == 'dictate':
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
                sender.sendto(json.dumps({'final': 'recognized\nwords'}).encode(), fields['sock'])
        return {'ok': True}
    with patch.dict(os.environ, {'BATTY_KILIX_STORAGE_HOME': str(root)}), \
            patch.object(voice, 'ensure_daemon'), \
            patch.object(voice, 'voice_request', side_effect=fake_voice_request), \
            patch.object(voice, 'active_pane', return_value=('private-control', {'id': 7})), \
            patch.object(voice, 'request') as paste:
        assert voice.main(['dictate', '--pane']) == 0
        assert ('private-control', 'paste', 7, b'recognized words') in [
            call.args for call in paste.call_args_list]
        assert all(call.args[1] != 'message' for call in paste.call_args_list)
        paste.reset_mock()
        assert voice.main(['dictate', '--socket', 'private-control', '--pane', '7']) == 0
        assert ('private-control', 'paste', 7, b'recognized words') in [
            call.args for call in paste.call_args_list]
        assert all(call.args[1] != 'message' for call in paste.call_args_list)
    with patch.object(voice, 'active_pane', return_value=('private-control', {'id': 7})), \
            patch.object(voice, 'ensure_daemon'), \
            patch.object(voice, 'dictate_once', side_effect=ValueError('model unavailable')), \
            patch.object(voice, 'notice') as failed_notice:
        try:
            voice.main(['dictate', '--socket', 'private-control', '--pane', '7'])
        except ValueError as error:
            assert str(error) == 'model unavailable'
        else:
            raise AssertionError('dictation failure was hidden')
        failed_notice.assert_called_once_with('private-control',
                                              'dictation failed: model unavailable')
    env = os.environ | {'BATTY_KILIX_STORAGE_HOME': str(root),
                        'KILIX_DATA_HOME': str(root / 'foreign-data'),
                        'KILIX_APP_AUTO_INSTALL': '0'}

    def cli(*args):
        return subprocess.run([str(ROOT / 'kilix'), 'app', *args], env=env,
                              capture_output=True, text=True, timeout=5)

    spec = app.application_spec('kilix-file')
    for utility in ('kilix-temps', 'kilix-memory', 'kilix-launcher'):
        selected = app.application_spec(utility)
        assert selected.install_id == spec.install_id == 'kilix-tui-utils'
        assert selected.ref == spec.ref
        assert selected.binary == f'.runtime/bin/{utility}'
        assert cli('ref', utility).stdout.strip() == spec.ref
        absent = cli('run', utility)
        assert absent.returncode == 1 and 'not installed' in absent.stderr
    assert cli('ref', 'kilix-file').stdout.strip() == spec.ref
    assert cli('ref', 'kilix-file').returncode == 0
    assert cli('ref', 'kilix-camera-wall').stdout.strip() == app.application_spec('kilix-nvr').ref
    assert cli('ref', 'kilix-object-detect').stdout.strip() == app.application_spec('kilix-object-detect').ref
    assert cli('ref', 'kilix-tmux-manager').stdout.strip() == app.TMUX_TUI_REF
    assert cli('ref', 'kilix-region-painter').stdout.strip() == app.MASK_REF
    assert cli('ref', 'kilix-model-store').stdout.strip() == app.BONSAI_REF
    assert cli('ref', 'kilix-chawan').stdout.strip() == app.CHAWAN_REF
    assert cli('ref', 'doom').returncode == 1
    assert cli('run', '../kilix-file').returncode == 1
    missing = cli('run', 'kilix-file')
    assert missing.returncode == 1 and 'not installed' in missing.stderr
    camera_missing = cli('run', 'kilix-camera-wall')
    assert camera_missing.returncode == 1 and 'not installed' in camera_missing.stderr
    tmux_missing = cli('run', 'kilix-tmux-manager')
    assert tmux_missing.returncode == 1 and 'not installed' in tmux_missing.stderr
    missing = subprocess.run([str(ROOT / 'kilix'), 'amp', '--help'], env=env,
                             capture_output=True, text=True, timeout=5)
    assert missing.returncode == 1 and 'kilix-amp' in missing.stderr
    rtsp_help = subprocess.run([str(ROOT / 'kilix'), 'rtsp', '--help'], env=env,
                               capture_output=True, text=True, timeout=5)
    assert rtsp_help.returncode == 0 and 'kilix rtsp' in rtsp_help.stdout
    camera_wrapper = subprocess.run([str(ROOT / 'kilix-rtsp'), '--help'], env=env,
                                    capture_output=True, text=True, timeout=5)
    assert camera_wrapper.returncode == 0 and 'kilix rtsp' in camera_wrapper.stdout
    assert subprocess.run([str(ROOT / 'kilix'), 'amp', '--install-only', 'extra'],
                          env=env, capture_output=True, timeout=5).returncode == 2
    mask_missing = cli('run', 'kilix-region-painter')
    assert mask_missing.returncode == 1 and 'not installed' in mask_missing.stderr
    bonsai_missing = cli('run', 'kilix-model-store')
    assert bonsai_missing.returncode == 1 and 'not installed' in bonsai_missing.stderr
    chawan_missing = cli('run', 'kilix-chawan')
    assert chawan_missing.returncode == 1 and 'not installed' in chawan_missing.stderr
    url_missing = subprocess.run([str(ROOT / 'kilix'), 'open-url', 'https://example.com'],
                                 env=env, capture_output=True, text=True, timeout=5)
    assert url_missing.returncode == 1 and 'kilix-chawan' in url_missing.stderr
    for verb, content_id in (('chawan', 'kilix-chawan'),
                             ('bonsai', 'kilix-model-store'),
                             ('mask', 'kilix-region-painter')):
        routed = subprocess.run([str(ROOT / 'kilix'), verb], env=env,
                                capture_output=True, text=True, timeout=5)
        assert routed.returncode == 1 and content_id in routed.stderr
    browse_missing = subprocess.run([str(ROOT / 'kilix'), 'open-url'], env=env,
                                    capture_output=True, text=True, timeout=5)
    assert browse_missing.returncode == 1 and 'kilix-chawan' in browse_missing.stderr
    without_display = subprocess.run([str(ROOT / 'kilix'), 'app', 'window', 'kilix-file'],
                                     env=env | {'KILIX_APP_AUTO_INSTALL': '1', 'DISPLAY': ''},
                                     capture_output=True, text=True, timeout=5)
    assert without_display.returncode == 1 and 'DISPLAY is required' in without_display.stderr
    assert not list(root.rglob('.git'))
    with patch.dict(os.environ, env):
        assert app.apps_root() == str(root / 'data/desktop-apps')
    assert not (root / 'foreign-data').exists()
    raster = root / 'soft-raster'
    for relative in ('build/demo', 'build/libsoft-raster.so',
                     'python/src/soft_raster/__init__.py'):
        target = raster / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    with patch.object(app, 'ensure_application', return_value=str(raster / 'build/demo')) as ensure:
        graphics_env = app.tui_graphics_environment()
        selected = ensure.call_args.args[0]
        assert selected.repository == app.SOFT_RASTER_REPOSITORY
        assert selected.ref == app.SOFT_RASTER_REF
        assert selected.build == ('make', 'all')
        assert graphics_env['SOFT_RASTER_LIBRARY'] == str(raster / 'build/libsoft-raster.so')
        assert graphics_env['PYTHONPATH'].split(os.pathsep) == [
            str(raster / 'python/src'), str(ROOT / 'third_party/kitty-frame-presenter/src')]
    unsafe = root / 'unsafe-storage'; unsafe.mkdir(); unsafe.chmod(0o755)
    with patch.dict(os.environ, {'BATTY_KILIX_STORAGE_HOME': str(unsafe)}):
        try:
            app.apps_root()
        except ValueError as error:
            assert 'private' in str(error)
        else:
            raise AssertionError('accepted non-private catalog storage')

    assert app.application_arguments(spec, ['--action', 'open', '--', 'file name']) == (
        'open', ['--open', 'file name'])
    for forwarded in (['--action'], ['--action', 'open', 'a', 'b'],
                      ['--action', 'missing']):
        try:
            app.application_arguments(spec, forwarded)
        except (ValueError, app.CatalogError):
            pass
        else:
            raise AssertionError(f'accepted invalid action: {forwarded}')

    executable = root / 'app'; executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o700)
    class ReadyInstaller:
        def __init__(self, directory):
            assert directory == str(root / 'data/desktop-apps')

        def ready(self, selected):
            assert selected.content_id == 'kilix-file'
            return str(executable)

    with patch.dict(os.environ, {'BATTY_KILIX_STORAGE_HOME': str(root),
                                 'KILIX_DATA_HOME': str(root / 'foreign-data'),
                                 'DISPLAY': ':99'}), \
            patch.object(app, 'Installer', ReadyInstaller), \
            patch.object(app.os, 'execvpe') as execute:
        assert app.main(['run', 'kilix-file', '--action', 'open', '--', 'file name']) == 0
        argv, supplied, child_env = execute.call_args.args
        assert argv == str(executable) and supplied == [str(executable), '--open', 'file name']
        assert child_env['KILIX_APP_ID'] == 'kilix-file'
        assert child_env['KILIX_APP_SURFACE'] == 'current'
        assert child_env['KILIX_APP_ACTION'] == 'open'
        execute.reset_mock()
        assert app.main(['window', 'kilix-file']) == 0
        argv, supplied, child_env = execute.call_args.args
        assert supplied == [str(ROOT / 'kilix'), '--ephemeral', '--', str(executable)]
        assert child_env['KILIX_APP_SURFACE'] == 'window'
        execute.reset_mock()
        assert app.main(['install', 'kilix-file']) == 0
        execute.assert_not_called()

    system = replace(app.application_spec('kilix-chawan'),
                     content_id='custom-system', command=(str(executable),))
    with patch.object(app, 'application_spec', return_value=system), \
            patch.object(app.os, 'execvpe') as execute:
        assert app.main(['run', 'custom-system']) == 0
        assert execute.call_args.args[1] == [str(executable)]

    with patch.dict(os.environ, {'BATTY_KILIX_STORAGE_HOME': str(root)}), \
            patch.object(app, 'ensure_application', return_value=str(executable)) as ensure, \
            patch.object(app.os, 'execvpe') as execute:
        assert app.main(['install', 'kilix-camera-wall']) == 0
        assert ensure.call_args.args[0].content_id == 'kilix-nvr'
        assert ensure.call_args.kwargs['install'] is True
        execute.assert_not_called()
        assert app.main(['run', 'kilix-camera-wall', '--action', 'camera', '--', 'front-door']) == 0
        assert ensure.call_args.kwargs['install'] is None
        assert execute.call_args.args[1] == [str(executable), 'view', 'front-door']
        camera_env = execute.call_args.args[2]
        assert camera_env['KILIX_NVR_HOME'] == str(root / 'data/kilix-nvr')
        assert camera_env['KILIX_RTSP_HOME'] == str(root / 'data/kilix-rtsp')
        assert camera_env['KILIX_LOOK_HOME'] == str(root / 'data/kilix-look')
        execute.reset_mock()
        with patch.dict(os.environ, {'DISPLAY': ':99'}):
            assert app.main(['window', 'kilix-camera-wall']) == 0
        assert execute.call_args.args[1] == [str(ROOT / 'kilix'), '--ephemeral', '--',
                                             str(executable), 'view']

    for content_id in ('kilix-nvr', 'kilix-object-detect'):
        with patch.dict(os.environ, {'BATTY_KILIX_STORAGE_HOME': str(root)}, clear=True), \
                patch.object(app, 'ensure_application', return_value=str(executable)), \
                patch.object(app.os, 'execvpe') as execute:
            assert app.main(['run', content_id, '--', 'classes']) == 0
            assert execute.call_args.args[2]['KILIX_OBJECT_DETECTOR'] == str(
                root / 'data/runtimes/yolo/bin/kilix-look-detect')
        with patch.dict(os.environ, {'BATTY_KILIX_STORAGE_HOME': str(root),
                                    'KILIX_OBJECT_DETECTOR': '/custom/detector'}, clear=True), \
                patch.object(app, 'ensure_application', return_value=str(executable)), \
                patch.object(app.os, 'execvpe') as execute:
            assert app.main(['run', content_id, '--', 'classes']) == 0
            assert execute.call_args.args[2]['KILIX_OBJECT_DETECTOR'] == '/custom/detector'

    with patch.dict(os.environ, {'BATTY_KILIX_STORAGE_HOME': str(root),
                                 'DISPLAY': ':99', 'TMUX_CLI': '/foreign/tb.py'}), \
            patch.object(app, 'ensure_application', return_value=str(executable)) as ensure, \
            patch.object(app.os, 'execvpe') as execute:
        assert app.main(['install', 'kilix-tmux-manager']) == 0
        selected = ensure.call_args.args[0]
        assert selected.repository == app.TMUX_TUI_REPOSITORY
        assert selected.ref == app.TMUX_TUI_REF
        assert selected.binary == 'tmux_tui.py'
        assert ensure.call_args.kwargs['install'] is True
        assert app.main(['run', 'kilix-tmux-manager']) == 0
        assert execute.call_args.args[1] == [str(executable)]
        assert execute.call_args.args[2]['TMUX_CLI'] == str(
            root / 'data/desktop-apps/kilix-tmux-manager/tmux-cli/tb.py')
        execute.reset_mock()
        assert app.main(['window', 'kilix-tmux-manager']) == 0
        assert execute.call_args.args[1] == [str(ROOT / 'kilix'), '--ephemeral',
                                             '--', str(executable)]

    with patch.dict(os.environ, {'BATTY_KILIX_STORAGE_HOME': str(root),
                                 'KILIX_AMP': '/foreign/amp',
                                 'KILIX_AMP_SOCKET': '/foreign/amp.sock',
                                 'KILIX_RTSP_HOME': '/foreign/rtsp',
                                 'KILIX_DATA_HOME': '/foreign/data',
                                 'PATH': '/foreign/bin'}), \
            patch.object(app, 'ensure_application', return_value=str(executable)), \
            patch.object(app.os, 'execvpe') as execute:
        assert app.main(['run', 'kilix-music-control']) == 0
        music_env = execute.call_args.args[2]
        assert music_env['PATH'] == str(ROOT) + os.pathsep + '/foreign/bin'
        assert music_env['KILIX_AMP'] == str(root / 'data/desktop-apps/kilix-amp/kilix-amp')
        assert music_env['KILIX_AMP_SOCKET'] == str(root / 'session/kilix-amp.sock')
        assert music_env['KILIX_DATA_HOME'] == str(root / 'data')
        assert (root / 'session').stat().st_mode & 0o077 == 0
        assert app.main(['run', 'kilix-camera-manager']) == 0
        camera_env = execute.call_args.args[2]
        assert camera_env['PATH'] == str(ROOT) + os.pathsep + '/foreign/bin'
        assert camera_env['KILIX_RTSP_HOME'] == str(root / 'data/kilix-rtsp')
        assert camera_env['KILIX_DATA_HOME'] == str(root / 'data')

    with patch.dict(os.environ, {'BATTY_KILIX_STORAGE_HOME': str(root),
                                 'DISPLAY': ':99'}), \
            patch.object(app, 'ensure_application', return_value=str(executable)) as ensure, \
            patch.object(app.os, 'execvpe') as execute:
        assert app.main(['install', 'kilix-region-painter']) == 0
        selected = ensure.call_args.args[0]
        assert selected.repository == app.MASK_REPOSITORY
        assert selected.ref == app.MASK_REF
        assert selected.build == ('make', 'all')
        assert selected.binary == 'build/kilix-mask'
        assert app.main(['run', 'kilix-region-painter', '--action', 'new']) == 0
        assert execute.call_args.args[1] == [str(executable), '--size', '640x400']
        image = root / 'room.png'
        image.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x00IEND\0\0\0\0')
        assert app.main(['run', 'kilix-region-painter', '--action', 'open', '--', str(image)]) == 0
        assert execute.call_args.args[1] == [str(executable), '--image', str(image)]
        mask_file = root / 'room.mask.png'
        marker = b'kilix-mask\0metadata'
        mask_file.write_bytes(b'\x89PNG\r\n\x1a\n' + len(marker).to_bytes(4, 'big') +
                              b'tEXt' + marker + b'\0\0\0\0')
        assert app.main(['run', 'kilix-region-painter', '--action', 'open', '--', str(mask_file)]) == 0
        assert execute.call_args.args[1] == [str(executable), str(mask_file)]
        for image_format, suffix in (('JPEG', '.jpg'), ('WEBP', '.webp')):
            picture = root / f'room{suffix}'
            Image.new('RGB', (3, 2), (210, 40, 80)).save(picture, format=image_format)
            original = picture.read_bytes()
            assert app.main(['run', 'kilix-region-painter', '--action', 'open', '--', str(picture)]) == 0
            plate = Path(execute.call_args.args[1][2])
            assert execute.call_args.args[1][:2] == [str(executable), '--image']
            assert plate.parent == root / 'cache/region-painter'
            assert plate.read_bytes().startswith(b'P6\n3 2\n255\n')
            assert plate.stat().st_mode & 0o077 == 0
            assert picture.read_bytes() == original
            assert app.mask_arguments('open', [str(picture)]) == ['--image', str(plate)]
        damaged = root / 'damaged.webp'
        damaged.write_bytes(b'RIFF\x00\x00\x00\x00WEBPbroken')
        execute.reset_mock()
        assert app.main(['run', 'kilix-region-painter', '--action', 'open', '--', str(damaged)]) == 1
        execute.assert_not_called()
        execute.reset_mock()
        assert app.main(['window', 'kilix-region-painter']) == 0
        assert execute.call_args.args[1] == [str(ROOT / 'kilix'), '--ephemeral',
                                             '--', str(executable), '--size', '640x400']

    with patch.dict(os.environ, {'BATTY_KILIX_STORAGE_HOME': str(root),
                                 'DISPLAY': ':99',
                                 'KILIX_BONSAI_BONSAI_8B_DIR': '/foreign/weights'}), \
            patch.object(app, 'ensure_application', return_value=str(executable)) as ensure, \
            patch.object(app.os, 'execvpe') as execute:
        assert app.main(['install', 'kilix-model-store']) == 0
        selected = ensure.call_args.args[0]
        assert selected.repository == app.BONSAI_REPOSITORY
        assert selected.ref == app.BONSAI_REF
        assert selected.binary == 'build/kilix-bonsai'
        assert app.main(['run', 'kilix-model-store', '--action', 'browse']) == 0
        assert execute.call_args.args[1] == [str(executable)]
        child_env = execute.call_args.args[2]
        assert child_env['KILIX_BONSAI_MODELS_DIR'] == str(root / 'data/models')
        assert child_env['GPU_TERMINAL_HOME'] == str(root)
        assert child_env['KILIX_DATA_HOME'] == str(root / 'data')
        assert 'KILIX_BONSAI_BONSAI_8B_DIR' not in child_env
        assert app.main(['window', 'kilix-model-store']) == 0
        assert execute.call_args.args[1] == [str(ROOT / 'kilix'), '--ephemeral',
                                             '--', str(executable)]

    with patch.dict(os.environ, {'BATTY_KILIX_STORAGE_HOME': str(root),
                                 'KILIX_TEMPS_STORAGE_HOME': '/foreign/temps'}), \
            patch.object(app, 'ensure_application', return_value=str(executable)) as ensure, \
            patch.object(app, 'tui_graphics_environment', return_value={
                'SOFT_RASTER_LIBRARY': '/private/libsoft-raster.so',
                'PYTHONPATH': '/private/soft-raster:/private/presenter'}) as graphics, \
            patch.object(app.os, 'execvpe') as execute:
        assert app.main(['run', 'kilix-temps', '--', '--graphics']) == 0
        assert ensure.call_args.args[0].install_id == 'kilix-tui-utils'
        assert execute.call_args.args[1] == [str(executable), '--graphics']
        assert execute.call_args.args[2]['KILIX_TEMPS_STORAGE_HOME'] == str(root / 'data/kilix-temps')
        assert execute.call_args.args[2]['KILIX_STREAM'] == '1'
        assert execute.call_args.args[2]['SOFT_RASTER_LIBRARY'] == '/private/libsoft-raster.so'
        assert execute.call_args.args[2]['PYTHONPATH'] == '/private/soft-raster:/private/presenter'
        assert app.main(['run', 'kilix-memory', '--', '--graphics']) == 0
        assert execute.call_args.args[1] == [str(executable), '--graphics']
        assert execute.call_args.args[2]['KILIX_STREAM'] == '1'
        assert graphics.call_count == 2
        assert app.main(['run', 'kilix-memory', '--', '--help']) == 0
        assert graphics.call_count == 2

    with patch.dict(os.environ, {'BATTY_KILIX_STORAGE_HOME': str(root),
                                 'DISPLAY': ':99', 'CHA_DIR': '/foreign/chawan'}), \
            patch.object(app, 'ensure_application', return_value=str(executable)) as ensure, \
            patch.object(app.os, 'execvpe') as execute:
        assert app.main(['install', 'kilix-chawan']) == 0
        selected = ensure.call_args.args[0]
        assert selected.repository == app.CHAWAN_REPOSITORY
        assert selected.ref == app.CHAWAN_REF
        assert selected.binary == 'target/release/bin/cha'
        assert app.main(['run', 'kilix-chawan', '--action', 'open', '--', 'about:chawan']) == 0
        assert execute.call_args.args[1] == [str(executable), '--', 'about:chawan']
        child_env = execute.call_args.args[2]
        config = root / 'config/chawan/config.toml'
        assert child_env['CHA_DIR'] == str(config.parent)
        assert config.read_bytes() == (ROOT / 'config/chawan.toml').read_bytes()
        config.write_text('custom = true\n')
        assert app.main(['run', 'kilix-chawan', '--action', 'browse']) == 0
        assert execute.call_args.args[1] == [str(executable), '-V']
        assert app.main(['window', 'kilix-chawan']) == 0
        assert execute.call_args.args[1] == [str(ROOT / 'kilix'), '--ephemeral',
                                             '--', str(executable), '-V']
        assert config.read_text() == 'custom = true\n'

    desktop_source = ROOT / 'third_party/kilix-desktop/src'
    sys.path.insert(0, str(desktop_source / 'config'))
    sys.path.insert(0, str(desktop_source / 'desktop'))
    with patch.dict(os.environ, {'BATTY_KILIX_DESKTOP': '1',
                                 'KILIX_HOME': str(desktop_source)}):
        import shell as desktop_shell
        with patch('shutil.which', return_value='/foreign/kilix-command'):
            host = str(desktop_source / 'kilix')
            assert desktop_shell.Shell.kilix_temps_target() == ([host, 'temps', '--graphics'], None)
            assert desktop_shell.Shell.kilix_memory_target() == ([host, 'memory', '--graphics'], None)
            assert desktop_shell.Shell.tmux_manager_target() == [host, 'tmux']
        desktop = desktop_shell.Shell.__new__(desktop_shell.Shell)
        desktop.batty_mode = True
        assert desktop.system_menu_items() == []
        with patch.dict(os.environ, {'KILIX_KITTEN': '', 'KITTY_LISTEN_ON': 'batty'}), \
                patch('shutil.which', return_value='/usr/bin/kitten'):
            assert desktop._kitten() is None
        with patch.dict(os.environ, {'KILIX_KITTEN': str(ROOT / 'batty-kitten')}):
            assert desktop._kitten() == str(ROOT / 'batty-kitten')

    games_dir = str(root / 'data/desktop-games')
    fake_games = SimpleNamespace(game_ready=lambda name: str(executable),
                                 ensure=lambda name, report: (str(executable), str(root / 'dosbox.conf')),
                                 GAMES_DIR=games_dir)
    with patch.dict(os.environ, {'BATTY_KILIX_STORAGE_HOME': str(root),
                                 'KILIX_DATA_HOME': str(root / 'foreign-data'),
                                 'KILIX_CONFIG_HOME': str(root / 'reference-config'),
                                 'KILIX_CACHE_HOME': str(root / 'reference-cache')}), \
            patch.dict(sys.modules, {'games': fake_games}):
        assert app.prepare_dosbox() == (str(executable), str(root / 'dosbox.conf'),
                                        games_dir)
        assert os.environ['KILIX_CONFIG_HOME'] == str(root / 'config')
        assert os.environ['KILIX_CACHE_HOME'] == str(root / 'cache')
        assert os.environ['KILIX_STATE_DIRECTORY'] == str(root / 'state')

    with patch.object(app, 'prepare_dosbox', return_value=(str(executable),
                                                           str(root / 'dosbox.conf'),
                                                           games_dir)) as prepare, \
            patch.object(app.os, 'execvpe') as execute:
        assert app.main(['install', 'dosbox']) == 0
        prepare.assert_called_with(install=True)
        execute.assert_not_called()
        assert app.main(['run', 'dosbox']) == 0
        argv = execute.call_args.args[1]
        assert argv == [str(ROOT / 'kilix'), 'run', '--fill', '--size', '640x400',
                        '--', str(executable), '-conf', str(root / 'dosbox.conf'),
                        '-c', f'mount c "{games_dir}"', '-c', 'c:']
        execute.reset_mock()
        with patch.dict(os.environ, {'DISPLAY': ':99'}):
            assert app.main(['window', 'dosbox']) == 0
        assert execute.call_args.args[1] == [str(ROOT / 'kilix'), '--ephemeral',
                                             '--', *argv]
        execute.reset_mock()
        assert app.main(['run', 'dosbox', '--', 'unexpected']) == 1
        execute.assert_not_called()

    with patch.dict(os.environ, {'BATTY_KILIX_STORAGE_HOME': str(root / 'desktop-storage')}), \
            patch.object(desktop_launch, 'inside_batty', return_value=False), \
            patch.object(desktop_launch.os, 'execve') as execute, \
            patch.object(sys, 'argv', ['kilix desktop']):
        desktop_launch.main()
        child_env = execute.call_args.args[2]
        assert child_env['KILIX_STORAGE_HOME'] == str(root / 'desktop-storage')
        assert child_env['KILIX_DATA_HOME'] == str(root / 'desktop-storage/data')
        assert child_env['KILIX_CONFIG_HOME'] == str(root / 'desktop-storage/config')
        assert child_env['KILIX_CACHE_HOME'] == str(root / 'desktop-storage/cache')
        assert child_env['KILIX_STATE_DIRECTORY'] == str(root / 'desktop-storage/state')
        assert child_env['KILIX_SESSION_HOME'] == str(root / 'desktop-storage/session')
        assert child_env['KILIX_KITTEN'] == str(ROOT / 'batty-kitten')
        assert child_env['KILIX_DESKTOP_FLAVOR'] == '95'
        assert child_env['KILIX_DESKTOP_DIR'] == str(root / 'desktop-storage/data/desktop')

    with patch.dict(os.environ, {'BATTY_KILIX_STORAGE_HOME': str(root / 'desktop-storage'),
                                 'KILIX_DESKTOP_DIR': str(root / 'foreign-desktop')}), \
            patch.object(desktop_launch, 'inside_batty', return_value=False), \
            patch.object(desktop_launch.os, 'execve') as execute, \
            patch.object(sys, 'argv', ['kilix desktop', 'xp']):
        desktop_launch.main()
        child_env = execute.call_args.args[2]
        assert child_env['KILIX_DESKTOP_FLAVOR'] == 'xp'
        assert child_env['KILIX_DESKTOP_DIR'] == str(root / 'desktop-storage/data/desktop-xp')

    with patch.dict(os.environ, {'BATTY_KILIX_STORAGE_HOME': str(root / 'desktop-storage')}), \
            patch.object(desktop_launch, 'inside_batty', return_value=True), \
            patch.object(desktop_launch.os, 'execv') as execute, \
            patch.object(sys, 'argv', ['kilix desktop', 'xp']):
        desktop_launch.main()
        assert execute.call_args.args[1][4:6] == ['--tab-title', 'kilix XP']
        assert 'KILIX_DESKTOP_FLAVOR=xp' in execute.call_args.args[1]

    document = root / 'sample.pdf'; document.write_bytes(b'%PDF fixture\n')
    with patch.dict(os.environ, {'BATTY_KILIX_STORAGE_HOME': str(root / 'desktop-storage')}), \
            patch.object(desktop_launch, 'inside_batty', return_value=False), \
            patch.object(desktop_launch.os, 'execve') as execute, \
            patch.object(sys, 'argv', ['kilix desktop', '--open', str(document)]):
        desktop_launch.main()
        assert execute.call_args.args[1][-2:] == ['--open', str(document)]

    checkout = root / 'nvr-checkout'
    source = checkout / nvr_build.SOURCE
    source.parent.mkdir(parents=True)
    original = b'void init(void) {\n' + nvr_build.OLD + b'\n'
    source.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    with patch.object(nvr_build, 'SOURCE_SHA256', digest), \
            patch.object(nvr_build.subprocess, 'run', return_value=type('Result', (),
                         {'returncode': 1})()):
        try:
            nvr_build.build(checkout)
        except RuntimeError as error:
            assert 'build failed' in str(error)
        else:
            raise AssertionError('accepted a failed NVR build')
    assert source.read_bytes() == original
    assert not (checkout / nvr_build.MARKER).exists()

    center_checkout = root / 'center-checkout'
    center_binary = center_checkout / '.runtime/bin/kilix-settings-center'
    center_binary.parent.mkdir(parents=True)
    center_binary.write_text('#!/bin/sh\n')
    center_env = {'BATTY_KILIX_STORAGE_HOME': str(root / 'center-storage'),
                  'KILIX_HOME': str(root / 'foreign-kilix'),
                  'GPU_TERMINAL_SOURCE_HOME': str(root / 'foreign-sources'),
                  'KILIX_RC_PASSWORD_FILE': str(root / 'foreign-password')}
    actual_launch = app.launch
    with patch.dict(os.environ, center_env), \
            patch.object(app, 'ensure_application', return_value=str(center_binary)), \
            patch.object(app, 'launch') as launch_center:
        assert app.main(['run', 'kilix-settings-center', '--action', 'desktop']) == 0
        center_command, center_spec, surface, action, overrides = launch_center.call_args.args
        assert center_command == [sys.executable, '-B', str(ROOT / 'tools/kilix_tui_entry.py'),
                                  str(center_checkout), '--app', 'settings',
                                  '--action', 'desktop']
        assert center_spec.content_id == 'kilix-settings-center'
        assert (surface, action) == ('current', 'desktop')
        assert overrides['KILIX_HOME'] == str(ROOT / 'third_party/kilix-desktop/src')
        assert overrides['GPU_TERMINAL_SOURCE_HOME'] == str(ROOT)
        assert overrides['KILIX_STORAGE_HOME'] == str(root / 'center-storage')
        assert app.main(['run', 'kilix-voice-studio', '--action', 'dictation']) == 0
        voice_command, voice_spec, _, _, _ = launch_center.call_args.args
        assert voice_command == [sys.executable, '-B', str(ROOT / 'tools/kilix_tui_entry.py'),
                                 str(center_checkout), '--app', 'voice',
                                 '--action', 'dictation']
        assert voice_spec.content_id == 'kilix-voice-studio'
        with patch.object(app.os, 'execvpe') as execute_center:
            actual_launch(center_command, center_spec, surface, action, overrides)
            child_env = execute_center.call_args.args[2]
            assert child_env['KILIX_HOME'] == overrides['KILIX_HOME']
            assert child_env['GPU_TERMINAL_SOURCE_HOME'] == str(ROOT)
            assert 'KILIX_RC_PASSWORD_FILE' not in child_env

print('PASS pinned application ref, disabled-install failure, actions, run/window routing, storage and desktop environment')
