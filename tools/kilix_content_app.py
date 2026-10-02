#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Install and run pinned Kilix catalog applications on Batty surfaces."""
import argparse
from dataclasses import replace
import fcntl
import hashlib
from io import BytesIO
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
TMUX_TUI_REPOSITORY = 'https://github.com/itsmygithubacct/tmux-tui.git'
TMUX_TUI_REF = 'e442022e82f26106c6ed12b4691fdfbf0ae21a3e'
MASK_REPOSITORY = 'https://github.com/itsmygithubacct/kilix-mask.git'
MASK_REF = '476bc1d62ad3cd37629906c97f9c2bb7404dd777'
BONSAI_REPOSITORY = 'https://github.com/itsmygithubacct/kilix-bonsai.git'
BONSAI_REF = 'b54e617968f63594bb5e6b887b4ed4e5a8b7f055'
CHAWAN_REPOSITORY = 'https://github.com/itsmygithubacct/kilix-chawan.git'
CHAWAN_REF = 'b2b2932453b1348be1ca841aaefd9258acdda0c1'
SOFT_RASTER_REPOSITORY = 'https://github.com/itsmygithubacct/soft-raster.git'
SOFT_RASTER_REF = '2b0c241729dec182a87ebab78edd10b142edf828'
CENTER_APPS = {'kilix-system-center': 'system',
               'kilix-settings-center': 'settings',
               'kilix-software-center': 'software',
               'kilix-session-center': 'session',
               'kilix-voice-studio': 'voice'}
sys.path.insert(0, str(ROOT / 'third_party/kilix-content/src'))
from kilix_content import ContentSpec, CatalogError, InstallError, Installer, default_catalog  # noqa: E402
from kilix_apps import private_directory  # noqa: E402


def apps_root():
    storage = os.environ.get('BATTY_KILIX_STORAGE_HOME')
    base = Path(storage).expanduser() if storage else Path(
        os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'batty/kilix'
    if not base.is_absolute():
        raise ValueError('Batty Kilix storage directory must be absolute')
    return str(Path(private_directory(base)) / 'data/desktop-apps')


def application_spec(content_id):
    if content_id in {'kilix-temps', 'kilix-memory', 'kilix-launcher'}:
        base = default_catalog().require('kilix-file')
        if base.package_id != 'kilix-tui-utils':
            raise CatalogError('kilix-tui-utils package differs from its pinned adapters')
        return replace(base, content_id=content_id,
                       label={'kilix-temps': 'Kilix Temps',
                              'kilix-memory': 'Kilix Memory',
                              'kilix-launcher': 'Kilix Launcher'}[content_id],
                       binary=f'.runtime/bin/{content_id}', actions=(), accepts=(),
                       preferred_size='900x600')
    spec = default_catalog().require(content_id)
    if spec.kind != 'app':
        raise CatalogError(f'{content_id} is {spec.kind!r} content, not an application')
    return spec


def application_arguments(spec, forwarded):
    values = list(forwarded)
    if values[:1] != ['--action']:
        if values[:1] == ['--']:
            values.pop(0)
        return '', values
    if len(values) < 2:
        raise ValueError('--action needs an action ID')
    action_id = values[1]
    inputs = values[2:]
    if inputs[:1] == ['--']:
        inputs.pop(0)
    action = spec.require_action(action_id)
    if len(inputs) > int(action.accepts_input):
        expected = 'at most one input' if action.accepts_input else 'no input'
        raise ValueError(f'{spec.content_id} action {action_id!r} accepts {expected}')
    return action_id, [*action.argv, *inputs]


def auto_install_enabled(content_id):
    value = os.environ.get('KILIX_APP_AUTO_INSTALL')
    if value is None and content_id == 'kilix-pdf-conversion':
        value = os.environ.get('KILIX_PDF_AUTO_INSTALL')
    return (value or '1').strip().casefold() in {'1', 'yes', 'true', 'on'}


def ensure_application(spec, *, install=None, force=False):
    installer = Installer(apps_root())
    if spec.content_id == 'kilix-nvr':
        return ensure_nvr_application(installer, spec, install, force=force)
    if force:
        return installer.reinstall(spec, lambda message: print(
            f'kilix app {spec.content_id}: {message}', file=sys.stderr))
    ready = installer.ready(spec)
    if ready:
        return ready
    allowed = auto_install_enabled(spec.content_id) if install is None else install
    if not allowed:
        raise InstallError(f'not installed under {apps_root()}; set KILIX_APP_AUTO_INSTALL=1 to build it')
    if spec.dependency_hint:
        print(f'kilix app {spec.content_id}: {spec.dependency_hint}', file=sys.stderr)
    return installer.ensure(spec, lambda message: print(
        f'kilix app {spec.content_id}: {message}', file=sys.stderr))


def ensure_nvr_application(installer, spec, install, *, force=False):
    """Require Batty's pinned NVR build fix even for older managed installs."""
    from kilix_nvr_build import ready as patched_ready

    builder = ROOT / 'tools/kilix_nvr_build.py'
    selected = replace(spec, build=(sys.executable, '-B', str(builder)))
    if force:
        executable = installer.reinstall(selected, lambda message: print(
            f'kilix app {spec.content_id}: {message}', file=sys.stderr))
        if not patched_ready(Path(executable).parent.parent):
            raise InstallError('Batty kilix-nvr build marker is invalid')
        return executable
    executable = installer.ready(selected)
    if executable and patched_ready(Path(executable).parent.parent):
        return executable
    if not executable:
        allowed = auto_install_enabled(spec.content_id) if install is None else install
        if not allowed:
            raise InstallError(f'not installed under {apps_root()}; set KILIX_APP_AUTO_INSTALL=1 to build it')
        if spec.dependency_hint:
            print(f'kilix app {spec.content_id}: {spec.dependency_hint}', file=sys.stderr)
        executable = installer.ensure(selected, lambda message: print(
            f'kilix app {spec.content_id}: {message}', file=sys.stderr))
        if not patched_ready(Path(executable).parent.parent):
            raise InstallError('Batty kilix-nvr build marker is invalid')
        return executable
    lock = Path(apps_root()) / '.kilix-nvr-batty.lock'
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'rb') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        current = installer.ready(selected)
        if current != executable:
            raise InstallError('kilix-nvr changed while preparing its Batty build')
        directory = Path(executable).parent.parent
        if not patched_ready(directory):
            try:
                result = subprocess.run([sys.executable, '-B', str(builder)],
                                        cwd=directory, capture_output=True, text=True,
                                        timeout=3600)
            except subprocess.TimeoutExpired as error:
                raise InstallError('Batty kilix-nvr build timed out') from error
            if result.returncode:
                raise InstallError('Batty kilix-nvr build failed: '
                                   + (result.stderr or result.stdout).strip()[-600:])
        if not patched_ready(directory):
            raise InstallError('Batty kilix-nvr build marker is invalid')
    return executable


def command_ready(command):
    first = command[0]
    if os.path.isabs(first):
        return first if os.path.isfile(first) and os.access(first, os.X_OK) else None
    return shutil.which(first)


def prepare_dosbox(*, install=False):
    """Use the bundled desktop's pinned DOSBox installer and private storage."""
    source = ROOT / 'third_party/kilix-desktop/src'
    data = Path(apps_root()).parent
    storage = data.parent
    os.environ.update(KILIX_HOME=str(source), KILIX_STORAGE_HOME=str(storage),
                      KILIX_DATA_HOME=str(data),
                      KILIX_CONFIG_HOME=str(storage / 'config'),
                      KILIX_CACHE_HOME=str(storage / 'cache'),
                      KILIX_STATE_DIRECTORY=str(storage / 'state'),
                      KILIX_SESSION_HOME=str(storage / 'session'))
    sys.path.insert(0, str(source / 'config'))
    sys.path.insert(0, str(source / 'desktop'))
    import games  # noqa: E402

    ready = games.game_ready('dosbox')
    if not ready and not (install or auto_install_enabled('dosbox')):
        raise InstallError(f'not installed under {data / "desktop-games"}; '
                           'set KILIX_APP_AUTO_INSTALL=1 to install it')
    executable, config = games.ensure('dosbox', report=lambda message: print(
        f'kilix app dosbox: {message}', file=sys.stderr))
    return executable, config, games.GAMES_DIR


def dosbox_argv(executable, config, games_dir):
    return [str(ROOT / 'kilix'), 'run', '--fill', '--size', '640x400', '--',
            executable, '-conf', config, '-c', f'mount c "{games_dir}"', '-c', 'c:']


def window_argv(spec, executable, arguments):
    if spec.launch_mode != 'terminal':
        return [executable, *arguments]
    return [str(ROOT / 'kilix'), '--ephemeral', '--', executable, *arguments]


def tui_graphics_environment():
    """Build the pinned native raster backend used by the two TUI dashboards."""
    spec = ContentSpec(content_id='soft-raster', label='Soft Raster', kind='app',
                       icon='', description='Raster backend for Kilix dashboards',
                       source_type='git', repository=SOFT_RASTER_REPOSITORY,
                       ref=SOFT_RASTER_REF, binary='build/demo', build=('make', 'all'),
                       dependency_hint='needs a C toolchain and make')
    demo = Path(ensure_application(spec))
    source = demo.parent.parent
    library = source / 'build/libsoft-raster.so'
    binding = source / 'python/src/soft_raster/__init__.py'
    presenter = ROOT / 'third_party/kitty-frame-presenter/src'
    if not library.is_file() or not binding.is_file() or not (
            presenter / 'kitty_frame_presenter/__init__.py').is_file():
        raise InstallError('pinned dashboard graphics dependencies are incomplete')
    return {'SOFT_RASTER_LIBRARY': str(library),
            'PYTHONPATH': os.pathsep.join((str(source / 'python/src'), str(presenter)))}


def mask_arguments(action, arguments):
    """Translate catalog actions to the pinned mask editor's actual CLI."""
    if action == 'new' or not arguments:
        return ['--size', '640x400']
    if action != 'open' or len(arguments) != 1:
        return arguments
    path = Path(arguments[0])
    if not path.is_file():
        raise ValueError(f'mask input does not exist: {path}')
    with path.open('rb') as stream:
        header = stream.read(12)
        if header[:3] == b'\xff\xd8\xff' or (header[:4] == b'RIFF' and header[8:] == b'WEBP'):
            return ['--image', mask_image_plate(path)]
        if header[:8] != b'\x89PNG\r\n\x1a\n':
            raise ValueError('mask input must be a PNG, JPEG, or WebP image or mask')
        stream.seek(8)
        while True:
            header = stream.read(8)
            if len(header) != 8:
                break
            length = int.from_bytes(header[:4], 'big')
            kind = header[4:]
            if kind == b'tEXt':
                if length > 1024 * 1024:
                    raise ValueError('mask PNG metadata is too large')
                if stream.read(length).startswith(b'kilix-mask\0'):
                    return arguments
            else:
                stream.seek(length, os.SEEK_CUR)
            if len(stream.read(4)) != 4 or kind == b'IEND':
                break
    return ['--image', *arguments]


def mask_image_plate(path):
    """Decode catalog JPEG/WebP inputs into a private, reusable PPM plate."""
    try:
        from PIL import Image, ImageOps
    except ImportError as error:
        raise InstallError('JPEG/WebP mask input needs Pillow (python3-pil)') from error
    with path.open('rb') as stream:
        content = stream.read(32 * 1024 * 1024 + 1)
    if len(content) > 32 * 1024 * 1024:
        raise ValueError('mask image exceeds the 32 MiB input limit')
    digest = hashlib.sha256(content).hexdigest()
    cache = Path(private_directory(Path(apps_root()).parent.parent / 'cache/region-painter'))
    target = cache / f'{digest}.ppm'
    try:
        info = target.lstat()
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077:
            return str(target)
        raise ValueError(f'unsafe cached mask plate: {target}')
    try:
        with Image.open(BytesIO(content)) as source:
            if source.format not in {'JPEG', 'WEBP'}:
                raise ValueError('mask input is not a JPEG or WebP image')
            if source.width * source.height > 16_000_000:
                raise ValueError('mask image exceeds the 16-megapixel limit')
            image = ImageOps.exif_transpose(source)
            if image.mode in {'RGBA', 'LA'} or 'transparency' in image.info:
                image = image.convert('RGBA')
                background = Image.new('RGB', image.size, 'white')
                background.paste(image, mask=image.getchannel('A'))
                image = background
            else:
                image = image.convert('RGB')
            descriptor, temporary = tempfile.mkstemp(prefix='.plate-', suffix='.ppm', dir=cache)
            try:
                with os.fdopen(descriptor, 'wb') as output:
                    image.save(output, format='PPM')
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError(f'cannot decode mask image: {error}') from error
    return str(target)


def chawan_config():
    storage = Path(apps_root()).parent.parent
    config = Path(private_directory(storage / 'config'))
    directory = Path(private_directory(config / 'chawan'))
    target = directory / 'config.toml'
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if not target.is_file() or target.is_symlink():
            raise ValueError(f'unsafe Chawan config path: {target}')
    else:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write((ROOT / 'config/chawan.toml').read_bytes())
    return str(directory)


def launch(argv, spec, surface, action, extra_environment=None):
    environment = os.environ.copy()
    environment.update(KILIX_APP_ID=spec.content_id, KILIX_APP_SURFACE=surface)
    if spec.content_id in {'kilix-camera-wall', 'kilix-nvr', 'kilix-rtsp',
                           'kilix-object-detect'}:
        data = Path(apps_root()).parent
        environment.update(KILIX_NVR_HOME=str(data / 'kilix-nvr'),
                           KILIX_RTSP_HOME=str(data / 'kilix-rtsp'),
                           KILIX_LOOK_HOME=str(data / 'kilix-look'))
        if spec.content_id in {'kilix-nvr', 'kilix-object-detect'}:
            environment.setdefault('KILIX_OBJECT_DETECTOR', str(
                data / 'runtimes/yolo/bin/kilix-look-detect'))
    if spec.content_id == 'kilix-tmux-manager':
        environment['TMUX_CLI'] = str(Path(apps_root()) / 'kilix-tmux-manager/tmux-cli/tb.py')
    if spec.content_id in {'kilix-music-control', 'kilix-camera-manager'}:
        data = Path(apps_root()).parent
        storage = data.parent
        inherited_path = environment.get('PATH', '')
        environment['PATH'] = str(ROOT) + (os.pathsep + inherited_path if inherited_path else '')
        environment.update(GPU_TERMINAL_HOME=str(storage),
                           KILIX_STORAGE_HOME=str(storage),
                           KILIX_DATA_HOME=str(data))
        if spec.content_id == 'kilix-music-control':
            session = Path(private_directory(storage / 'session'))
            environment['KILIX_AMP'] = str(Path(apps_root()) / 'kilix-amp/kilix-amp')
            environment['KILIX_AMP_SOCKET'] = str(session / 'kilix-amp.sock')
        else:
            environment['KILIX_RTSP_HOME'] = str(data / 'kilix-rtsp')
    if spec.content_id == 'kilix-model-store':
        data = Path(apps_root()).parent
        storage = data.parent
        for key in tuple(environment):
            if key.startswith('KILIX_BONSAI_') and key.endswith('_DIR'):
                environment.pop(key)
        environment.update(GPU_TERMINAL_HOME=str(storage),
                           KILIX_STORAGE_HOME=str(storage),
                           KILIX_DATA_HOME=str(data),
                           KILIX_BONSAI_MODELS_DIR=str(data / 'models'))
    if spec.content_id == 'kilix-chawan':
        environment['CHA_DIR'] = chawan_config()
    if spec.content_id in {'kilix-temps', 'kilix-memory'}:
        environment['KILIX_STREAM'] = '1'
        if spec.content_id == 'kilix-temps':
            environment['KILIX_TEMPS_STORAGE_HOME'] = str(Path(apps_root()).parent / 'kilix-temps')
    if spec.content_id == 'kilix-launcher':
        data = Path(apps_root()).parent
        storage = data.parent
        environment.update(KILIX_HOME=str(ROOT / 'third_party/kilix-desktop/src'),
                           KILIX_STORAGE_HOME=str(storage),
                           KILIX_DATA_HOME=str(data),
                           KILIX_DESKTOP_DIR=str(data / 'desktop'))
        environment['PATH'] = str(ROOT) + os.pathsep + environment.get('PATH', '')
    if spec.content_id in CENTER_APPS:
        environment.pop('KILIX_RC_PASSWORD_FILE', None)
    if action:
        environment['KILIX_APP_ACTION'] = action
    if extra_environment:
        environment.update(extra_environment)
    os.execvpe(argv[0], argv, environment)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='kilix app', description=__doc__)
    parser.add_argument('verb', choices=('run', 'window', 'install', 'reinstall', 'ref'))
    parser.add_argument('content_id')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    forwarded = list(args.arguments)
    if forwarded[:1] == ['--']:
        forwarded.pop(0)
    if args.verb in {'install', 'reinstall', 'ref'} and forwarded:
        parser.error(f'{args.verb} does not accept application arguments')
    try:
        spec = application_spec(args.content_id)
        if args.verb == 'reinstall':
            if spec.content_id not in {'kilix-nvr', 'kilix-rtsp', 'kilix-object-detect'}:
                raise InstallError('force reinstall is currently supported only for kilix-nvr, kilix-rtsp and kilix-object-detect')
            print(ensure_application(spec, install=True, force=True))
            return 0
        if args.verb == 'ref':
            refs = {'kilix-tmux-manager': TMUX_TUI_REF,
                    'kilix-region-painter': MASK_REF,
                    'kilix-model-store': BONSAI_REF,
                    'kilix-chawan': CHAWAN_REF}
            ref = (application_spec('kilix-nvr').ref if spec.content_id == 'kilix-camera-wall'
                   else refs.get(spec.content_id, spec.ref))
            if not ref:
                raise CatalogError(f'{spec.content_id} has no immutable catalog ref')
            print(ref)
            return 0
        if args.verb == 'window' and not os.environ.get('DISPLAY'):
            raise RuntimeError('a DISPLAY is required for the window surface')
        action, arguments = application_arguments(spec, forwarded)
        if spec.source_type == 'system':
            command = list(spec.command or (spec.binary or spec.content_id,))
            if spec.content_id == 'kilix-camera-wall':
                if command != ['kilix', 'nvr', 'view']:
                    raise CatalogError('kilix-camera-wall host command differs from its pinned NVR adapter')
                nvr = application_spec('kilix-nvr')
                executable = ensure_application(nvr, install=True if args.verb == 'install' else None)
                if args.verb == 'install':
                    print(executable)
                    return 0
                command = [executable, 'view', *arguments]
            elif spec.content_id == 'kilix-tmux-manager':
                if command != ['kilix', 'tmux']:
                    raise CatalogError('kilix-tmux-manager host command differs from its pinned adapter')
                tmux = replace(spec, source_type='git', repository=TMUX_TUI_REPOSITORY,
                               ref=TMUX_TUI_REF, binary='tmux_tui.py', command=(),
                               dependency_hint='needs Python 3 and tmux')
                executable = ensure_application(tmux, install=True if args.verb == 'install' else None)
                if args.verb == 'install':
                    print(executable)
                    return 0
                command = [executable, *arguments]
            elif spec.content_id == 'kilix-region-painter':
                if command != ['kilix', 'mask']:
                    raise CatalogError('kilix-region-painter host command differs from its pinned adapter')
                mask = replace(spec, source_type='git', repository=MASK_REPOSITORY,
                               ref=MASK_REF, binary='build/kilix-mask', command=(),
                               build=('make', 'all'),
                               dependency_hint='needs a C toolchain and zlib development files')
                executable = ensure_application(mask, install=True if args.verb == 'install' else None)
                if args.verb == 'install':
                    print(executable)
                    return 0
                command = [executable, *mask_arguments(action, arguments)]
            elif spec.content_id == 'kilix-model-store':
                if command != ['kilix', 'bonsai']:
                    raise CatalogError('kilix-model-store host command differs from its pinned adapter')
                bonsai = replace(spec, source_type='git', repository=BONSAI_REPOSITORY,
                                 ref=BONSAI_REF, binary='build/kilix-bonsai', command=(),
                                 build=(sys.executable, '-B', str(ROOT / 'tools/kilix_bonsai_build.py')),
                                 dependency_hint='needs Python 3; model weights are installed separately')
                executable = ensure_application(bonsai, install=True if args.verb == 'install' else None)
                if args.verb == 'install':
                    print(executable)
                    return 0
                command = [executable, *arguments]
            elif spec.content_id == 'kilix-chawan':
                if command != ['kilix', 'chawan']:
                    raise CatalogError('kilix-chawan host command differs from its pinned adapter')
                chawan = replace(spec, source_type='git', repository=CHAWAN_REPOSITORY,
                                 ref=CHAWAN_REF, binary='target/release/bin/cha', command=(),
                                 build=(sys.executable, '-B', str(ROOT / 'tools/kilix_chawan_build.py')),
                                 dependency_hint='needs curl, a C toolchain, OpenSSL and Brotli development files')
                executable = ensure_application(chawan, install=True if args.verb == 'install' else None)
                if args.verb == 'install':
                    print(executable)
                    return 0
                command = ([executable, '--', *arguments] if action == 'open' and arguments
                           else [executable, *(arguments or ['-V'])])
            else:
                if command[0] == 'kilix':
                    if len(command) > 1 and command[1] in {'bonsai', 'nvr', 'mask', 'chawan', 'tmux'}:
                        raise InstallError(f'Kilix host command {command[1]!r} has not been ported to Batty')
                    command[0] = str(ROOT / 'kilix')
                ready = command_ready(command)
                if args.verb == 'install':
                    if not ready:
                        raise InstallError(f'system command {command[0]!r} is not installed')
                    print(ready)
                    return 0
                if not ready:
                    raise InstallError(f'system command {command[0]!r} is not installed')
                command = [ready, *command[1:], *arguments]
        elif spec.source_type in {'git', 'archive'}:
            executable = ensure_application(spec, install=True if args.verb == 'install' else None)
            if args.verb == 'install':
                print(executable)
                return 0
            command = [executable, *arguments]
        elif spec.source_type == 'custom' and spec.content_id == 'dosbox':
            if arguments:
                raise ValueError('dosbox does not accept application arguments')
            executable, config, games_dir = prepare_dosbox(install=args.verb == 'install')
            if args.verb == 'install':
                print(executable)
                return 0
            command = dosbox_argv(executable, config, games_dir)
            if args.verb == 'window':
                command = [str(ROOT / 'kilix'), '--ephemeral', '--', *command]
        else:
            raise CatalogError(f'{spec.content_id} has no Batty application launcher for {spec.source_type!r}')
        if spec.content_id == 'kilix-launcher':
            command = [sys.executable, '-B', str(ROOT / 'tools/kilix_launcher.py'),
                       command[0], *command[1:]]
        center_environment = None
        if spec.content_id in CENTER_APPS:
            if spec.package_id != 'kilix-tui-utils':
                raise CatalogError('focused center package differs from the pinned TUI')
            checkout = Path(executable).parent.parent.parent.resolve(strict=True)
            storage = Path(apps_root()).parent.parent
            command = [sys.executable, '-B', str(ROOT / 'tools/kilix_tui_entry.py'),
                       str(checkout), '--app', CENTER_APPS[spec.content_id], *arguments]
            center_environment = {'KILIX_HOME': str(ROOT / 'third_party/kilix-desktop/src'),
                                  'KILIX_KITTEN': str(ROOT / 'batty-kitten'),
                                  'KITTY_LISTEN_ON': 'batty',
                                  'KILIX_STORAGE_HOME': str(storage),
                                  'KILIX_DATA_HOME': str(storage / 'data'),
                                  'KILIX_CONFIG_HOME': str(storage / 'config'),
                                  'KILIX_CACHE_HOME': str(storage / 'cache'),
                                  'KILIX_STATE_DIRECTORY': str(storage / 'state'),
                                  'KILIX_SESSION_HOME': str(storage / 'session'),
                                  'GPU_TERMINAL_HOME': str(storage),
                                  'GPU_TERMINAL_SOURCE_HOME': str(ROOT),
                                  'PATH': str(ROOT) + os.pathsep + os.environ.get('PATH', '')}
        if args.verb == 'window' and spec.content_id != 'dosbox':
            command = window_argv(spec, command[0], command[1:])
        graphics_environment = (tui_graphics_environment()
                                if spec.content_id in {'kilix-temps', 'kilix-memory'}
                                and '--graphics' in arguments else None)
        launch(command, spec, 'window' if args.verb == 'window' else 'current', action,
               center_environment or graphics_environment)
    except (CatalogError, InstallError, OSError, RuntimeError, ValueError) as error:
        print(f'kilix app {args.content_id}: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
