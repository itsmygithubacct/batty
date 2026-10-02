#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build and launch pinned Kitty-graphics Kilix desktop providers in Batty."""
import argparse
import os
from pathlib import Path
import stat
import subprocess
import sys

import kilix_content_app as app
from kilix_apps import inside_batty, private_directory

ROOT = Path(__file__).resolve().parent.parent
BUNDLED = ROOT / 'third_party/kilix-desktop/src'
PROVIDERS = {
    'cap': ('Kilix Cap', 'https://github.com/itsmygithubacct/kilix-cap.git',
            '074e9c1559a696c95095215d12c21795452f1933', 'bin/kilix-cap'),
    'land': ('Kilix Land', 'https://github.com/itsmygithubacct/kilix-land-desktop.git',
             '631b0d7f6da1cc1b8ff8656fffd8f2a9195df39d', 'kilix-land-desktop'),
    'icewm': ('Kilix IceWM', 'https://github.com/itsmygithubacct/kilix-icewm.git',
              '0b9f11b45fddc5370c37b00e9cd9e42ac5a5f6d7', 'bin/kilix-icewm'),
}


def provider_spec(name):
    label, repository, ref, binary = PROVIDERS[name]
    return app.ContentSpec(content_id=f'kilix-{name}-desktop', label=label,
                           kind='app', icon='', description=f'{label} provider',
                           source_type='git', repository=repository, ref=ref,
                           binary=binary, build=(() if name == 'icewm' else
                                                  ('make', '--no-print-directory')))


def storage_home():
    data = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    return Path(private_directory(os.environ.get('BATTY_KILIX_STORAGE_HOME',
                                                  data / 'batty/kilix')))


def compatibility_marker(storage):
    """The upstream C launchers require a readable Kitty password file.

    Batty control authenticates by its own local socket, and batty-kitten
    ignores --password-file. The empty private file only enables those
    launchers' session checks; it is never used as a credential.
    """
    directory = Path(private_directory(storage / 'config'))
    path = directory / 'batty-kitten-compat'
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('unsafe Batty control compatibility marker')
    return str(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, prog='kilix desktop')
    parser.add_argument('provider', choices=PROVIDERS)
    parser.add_argument('--install-only', action='store_true')
    args, forwarded = parser.parse_known_args(argv)
    spec = provider_spec(args.provider)
    storage = storage_home()
    installer = app.Installer(str(Path(private_directory(storage / 'data')) / 'desktop-providers'))
    executable = Path(installer.ensure(spec, lambda message: print(
        f'kilix {args.provider}: {message}', file=sys.stderr)))
    checkout = executable.parent.parent if args.provider in ('cap', 'icewm') else executable.parent
    icewm_storage = None
    if args.provider == 'icewm':
        icewm_storage = Path(private_directory(storage / 'data/icewm'))
        build_env = os.environ.copy()
        build_env['KILIX_ICEWM_STORAGE_HOME'] = str(icewm_storage)
        build_env['KILIX_HOME'] = str(BUNDLED)
        build_env.pop('KILIX_ICEWM_PREFIX', None)
        builder = checkout / 'scripts/build-icewm.sh'
        if not builder.is_file() or builder.is_symlink():
            raise ValueError('pinned IceWM builder is missing or unsafe')
        subprocess.run(['bash', str(builder), '--print-path'], env=build_env,
                       check=True, timeout=3600, stdout=subprocess.DEVNULL)
    if args.install_only:
        print(executable)
        return 0
    marker = compatibility_marker(storage)
    overrides = {
        'KILIX_HOME': str(BUNDLED), 'KILIX_KITTEN': str(ROOT / 'batty-kitten'),
        'KITTY_LISTEN_ON': 'batty', 'KILIX_RC_PASSWORD_FILE': marker,
        'KILIX_STORAGE_HOME': str(storage), 'KILIX_DATA_HOME': str(storage / 'data'),
        'KILIX_CONFIG_HOME': str(storage / 'config'),
        'KILIX_CACHE_HOME': str(storage / 'cache'),
        'KILIX_STATE_DIRECTORY': str(storage / 'state'),
        'KILIX_SESSION_HOME': str(storage / 'session'),
        'GPU_TERMINAL_SOURCE_HOME': str(ROOT),
        'PATH': str(ROOT) + os.pathsep + os.environ.get('PATH', ''),
    }
    if args.provider == 'cap':
        overrides['KILIX_CAP_CONFIG_HOME'] = str(Path(private_directory(storage / 'config/cap')))
        overrides['KILIX_CAP_ASSET_DIR'] = str(checkout / 'assets/sfx')
        overrides['KILIX_CAP_VISUAL_DIR'] = str(checkout / 'assets/art')
    elif args.provider == 'land':
        overrides['KILIX_LAND_DESKTOP_ASSETS'] = str(checkout)
        overrides['KILIX_LAND_DESKTOP_CONFIG_HOME'] = str(
            Path(private_directory(storage / 'config/land')))
    else:
        overrides['KILIX_ICEWM_STORAGE_HOME'] = str(icewm_storage)
        overrides['KILIX_ICEWM_PREFIX'] = str(icewm_storage / 'prefix')
        overrides['KILIX_COMMAND'] = str(ROOT / 'kilix')
        overrides['KILIX_TERMINAL'] = str(ROOT / 'kilix')
    child = [str(executable), *forwarded]
    if inside_batty():
        command = [str(ROOT / 'kilix'), 'launch', '--type=tab', '--self',
                   '--tab-title', PROVIDERS[args.provider][0]]
        for name, value in overrides.items():
            command += ['--env', f'{name}={value}']
        os.execv(command[0], [*command, '--', *child])
    environment = os.environ.copy()
    environment.update(overrides)
    os.execve(str(ROOT / 'kilix'), [str(ROOT / 'kilix'), '--initial-title',
                                  PROVIDERS[args.provider][0], '--', *child], environment)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError, app.InstallError) as error:
        print(f'kilix desktop: {error}', file=sys.stderr)
        raise SystemExit(1)
