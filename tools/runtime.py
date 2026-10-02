#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build the remote bash-os revision and keep the terminal's Bash ABI paired with it."""
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile


class RuntimeErrorDetail(Exception):
    pass


def clean_environment():
    # These affect noninteractive Bash even with --noprofile and --norc.
    return {k: v for k, v in os.environ.items()
            if k not in ('BASH_ENV', 'ENV', 'SHELLOPTS', 'BASHOPTS')
            and not k.startswith('BASH_FUNC_')}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(data, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


class Runtime:
    artifacts = ('batty.so', 'batty-session', 'batty-state', 'session-test', 'window-test', 'cursor-test', 'views-test',
                 'workspace-test', 'persistence-test', 'graphics-test', 'sixel-test', 'layout-test')

    def __init__(self, root, env=None):
        self.root = Path(root).resolve()
        self.env = dict(clean_environment() if env is None else env)
        self.cache = self.root / '.cache'
        self.record = self.root / 'build/bash-os.json'
        self.repository = Path(self.env.get('BATTY_BASH_OS_SOURCE',
                                           str(Path.home() / 'projects/bash-os'))).expanduser().resolve()
        self.remote = self.env.get('BATTY_BASH_OS_REMOTE', 'origin')
        self.ref = self.env.get('BATTY_BASH_OS_REF', 'HEAD')
        self.offline = self.env.get('BATTY_OFFLINE', '0') == '1'

    @contextlib.contextmanager
    def locked(self):
        self.cache.mkdir(parents=True, exist_ok=True)
        with (self.cache / 'runtime.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def run(self, argv, *, cwd=None, capture=False, timeout=None, env=None, stdout=None):
        try:
            return subprocess.run(argv, cwd=cwd, env=env or self.env, check=True,
                                  text=True, timeout=timeout,
                                  stdout=subprocess.PIPE if capture else stdout or sys.stderr,
                                  stderr=subprocess.PIPE if capture else None)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeErrorDetail('Command timed out: ' + str(argv[0])) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or '').strip()
            raise RuntimeErrorDetail(f'{Path(argv[0]).name} failed (exit {exc.returncode})'
                                     + (f': {detail}' if detail else '')) from exc

    def fetch(self):
        if not (self.repository / '.git').exists():
            raise RuntimeErrorDetail(f'bash-os checkout not found: {self.repository}. '
                                     'Set BATTY_BASH_OS_SOURCE to its checkout.')
        if not self.remote or self.remote.startswith('-') or not self.ref or self.ref.startswith('-'):
            raise RuntimeErrorDetail('Invalid bash-os remote or ref.')
        url = self.run(['git', '-C', str(self.repository), 'remote', 'get-url', self.remote],
                       capture=True).stdout.strip()
        # Resolve local remotes relative to their checkout, as git -C would.
        if ':' not in url and not Path(url).is_absolute():
            url = str((self.repository / url).resolve())
        mirror = self.cache / 'bash-os.git'
        if not mirror.exists():
            self.run(['git', 'init', '--bare', '--quiet', str(mirror)])
        network_env = self.env | {'GIT_TERMINAL_PROMPT': '0'}
        print(f'Batty: checking bash-os {self.remote}/{self.ref}', file=sys.stderr, flush=True)
        try:
            self.run(['git', '-C', str(mirror), 'fetch', '--quiet', '--depth=1', '--no-tags',
                      '--no-recurse-submodules', '--no-write-fetch-head', '--', url,
                      f'+{self.ref}:refs/batty/latest'], env=network_env, timeout=45)
        except RuntimeErrorDetail as exc:
            raise RuntimeErrorDetail(f'Cannot check the latest bash-os remote: {exc}. '
                                     'Use BATTY_OFFLINE=1 explicitly to reuse a verified build.') from exc
        revision = self.run(['git', '-C', str(mirror), 'rev-parse',
                             'refs/batty/latest^{commit}'], capture=True).stdout.strip()
        if not re.fullmatch(r'[0-9a-f]{40,64}', revision):
            raise RuntimeErrorDetail('Remote did not resolve to a commit.')
        return mirror, revision

    def source(self, mirror, revision):
        source = self.cache / 'bash-os' / revision
        if not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.source-', dir=source.parent) as scratch:
                scratch = Path(scratch)
                archive = scratch / 'source.tar'
                with archive.open('wb') as stream:
                    self.run(['git', '-C', str(mirror), 'archive', revision], stdout=stream)
                extracted = scratch / 'tree'
                extracted.mkdir()
                with tarfile.open(archive) as stream:
                    stream.extractall(extracted, filter='data')
                extracted.rename(source)
        # Reuse only download files that bash-os itself checks against pinned hashes.
        for pattern in ('bash-*.tar.gz', 'patches/bash[0-9]*-[0-9]*'):
            for original in (self.repository / 'dl').glob(pattern):
                target = source / 'dl' / original.relative_to(self.repository / 'dl')
                if original.is_file() and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original, target)
        return source

    def validate(self, source):
        binary = source / 'out/bash'
        headers = source / 'build/bash-5.3'
        manifest = read_json(source / 'out/bash.manifest.json')
        if not manifest or manifest.get('base') != 'full':
            raise RuntimeErrorDetail('bash-os full-profile manifest is missing.')
        names = manifest.get('names', [])
        if not {'pty', 'vt', 'bashpoll', 'gpu', 'ls'}.issubset(names):
            raise RuntimeErrorDetail('bash-os full profile is missing its required builtins.')
        if not os.access(binary, os.X_OK) or digest(binary) != manifest.get('binary_sha256'):
            raise RuntimeErrorDetail('bash-os executable does not match its build manifest.')
        header_digest = hashlib.sha256()
        for name in ('config.h', 'builtins.h', 'version.h'):
            header = headers / name
            if not header.is_file():
                raise RuntimeErrorDetail('bash-os configured headers are missing.')
            header_digest.update(name.encode() + b'\0' + header.read_bytes())
        if not re.search(r'#define\s+DISTVERSION\s+"5\.3"', (headers / 'version.h').read_text()):
            raise RuntimeErrorDetail('Batty currently requires bash-os based on Bash 5.3.')
        probe = '''[[ ${BASH_VERSINFO[0]} == 5 && ${BASH_VERSINFO[1]} == 3 ]] || exit 1
for name; do [[ $(type -t -- "$name") == builtin ]] || exit 1; done
printf '%s\\n' "$BASH_VERSION"
'''
        version = self.run([str(binary), '--noprofile', '--norc', '-c', probe, 'batty-runtime',
                            *names], capture=True, timeout=15).stdout.strip()
        return dict(binary=str(binary), headers=str(headers), sha256=manifest['binary_sha256'],
                    headers_sha256=header_digest.hexdigest(), version=version,
                    profile='full', builtins=len(names))

    def prepare(self, force=False):
        if self.offline:
            previous = read_json(self.record)
            if not previous:
                raise RuntimeErrorDetail('No verified bash-os build is available offline. Run ./build.sh online first.')
            source = self.cache / 'bash-os' / previous['revision']
            current = self.validate(source)
            if any(current[k] != previous.get(k) for k in current):
                raise RuntimeErrorDetail('The cached bash-os build changed; run ./build.sh online to verify it.')
            print(f'Batty: offline bash-os {previous["revision"][:12]}', file=sys.stderr)
            return previous
        mirror, revision = self.fetch()
        source = self.source(mirror, revision)
        repair = False
        try:
            details = self.validate(source)
        except (RuntimeErrorDetail, OSError):
            repair = True
        if force or repair:
            print(f'Batty: building bash-os {revision[:12]} (full profile)', file=sys.stderr, flush=True)
            build_env = dict(self.env)
            # Build the upstream profile, without injecting additional local loadables
            # or changing its host configure into a cross build.
            for key in ('EXTRA_LOADABLES', 'CONFIGURE_EXTRA', 'BASH_TARBALL'):
                build_env.pop(key, None)
            build_env['JOBS'] = self.env.get('BUILD_JOBS', '4')
            self.run(['/bin/bash', '--noprofile', '--norc', './build.sh', '--profile', 'full',
                      *(['--clean'] if repair else [])],
                     cwd=source, env=build_env)
            details = self.validate(source)
        return details | dict(schema=1, revision=revision, source=str(self.repository),
                              remote=self.remote, ref=self.ref,
                              checked_at=datetime.now(timezone.utc).isoformat())

    def ensure_native(self, runtime, force=False):
        previous = read_json(self.record) or {}
        keys = ('schema', 'revision', 'sha256', 'headers_sha256', 'binary', 'headers')
        rebuild = force or any(runtime[k] != previous.get(k) for k in keys)
        if rebuild or any(not (self.root / 'build' / name).is_file() for name in self.artifacts):
            self.run(['/bin/bash', '--noprofile', '--norc', str(self.root / 'tools/build-native.sh')],
                     env=self.env | {'BASH_SOURCE_DIR': runtime['headers']})
        # Publish the runtime choice only after its matching native build succeeds.
        write_json(self.record, runtime)

    def environment(self, runtime):
        return self.env | {'BATTY_BASH': runtime['binary'], 'SHELL': runtime['binary'],
                           'BATTY_BASH_OS_REVISION': runtime['revision']}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ('build', 'launch', 'kilix'):
        raise RuntimeErrorDetail('Usage: runtime.py build | launch | kilix [ARGUMENTS...]')
    action, args = sys.argv[1], sys.argv[2:]
    if action == 'build' and args:
        raise RuntimeErrorDetail('Usage: ./build.sh')
    runtime = Runtime(Path(__file__).resolve().parent.parent)
    with runtime.locked():
        selected = runtime.prepare(force=action == 'build')
        runtime.ensure_native(selected, force=action == 'build')
    if action in ('launch', 'kilix'):
        if args == ['--runtime-info']:
            print(json.dumps(selected, indent=2))
            return
        binary = selected['binary']
        os.execve(binary, [binary, '--noprofile', '--norc', str(runtime.root / ('lib/kilix.bash' if action == 'kilix' else 'lib/controller.bash')),
                           *args], runtime.environment(selected))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeErrorDetail, OSError) as exc:
        print(f'Batty: {exc}', file=sys.stderr)
        sys.exit(1)
