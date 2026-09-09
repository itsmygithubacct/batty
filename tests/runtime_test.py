#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise updates and failures against private Git remotes, using a real bash-os binary."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from runtime import Runtime, RuntimeErrorDetail, clean_environment, digest, write_json


# The fixture build copies the verified executable instead of compiling Bash in
# each test. Git fetch/archive, runtime verification, publication and exec are real.
UPSTREAM_BUILD = '''set -euo pipefail
python3 - <<'PY'
import os, shutil
from pathlib import Path
source = Path(os.environ['BATTY_TEST_RUNTIME_SOURCE'])
Path('out').mkdir(exist_ok=True)
for name in ('bash', 'bash.manifest.json'):
    shutil.copy2(source / 'out' / name, Path('out') / name)
headers = Path('build/bash-5.3')
headers.mkdir(parents=True, exist_ok=True)
for name in ('config.h', 'builtins.h', 'version.h'):
    shutil.copy2(source / 'build/bash-5.3' / name, headers / name)
count = Path('build-count')
count.write_text(str(int(count.read_text()) + 1 if count.exists() else 1))
PY
'''

NATIVE_BUILD = '''set -euo pipefail
cd -- "$(dirname -- "$0")/.."
[[ ! -f fail-native ]] || exit 24
python3 - <<'PY'
import os
from pathlib import Path
Path('build').mkdir(exist_ok=True)
for name in ('batty.so', 'batty-session', 'batty-state', 'session-test', 'window-test',
             'persistence-test', 'graphics-test', 'sixel-test'):
    (Path('build') / name).write_text(os.environ['BASH_SOURCE_DIR'])
count = Path('native-build-count')
count.write_text(str(int(count.read_text()) + 1 if count.exists() else 1))
PY
'''


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        record = json.loads((ROOT / 'build/bash-os.json').read_text())
        cls.verified_source = str(Path(record['binary']).parent.parent)

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='batty-runtime-test-')
        self.addCleanup(self.scratch.cleanup)
        self.base = Path(self.scratch.name)
        self.root = self.base / "terminal with spaces ' and $literal"
        (self.root / 'tools').mkdir(parents=True)
        (self.root / 'lib').mkdir()
        (self.root / 'tools/build-native.sh').write_text(NATIVE_BUILD)
        shutil.copy2(ROOT / 'tools/runtime.py', self.root / 'tools/runtime.py')
        (self.root / 'lib/controller.bash').write_text(
            'printf "%s\\n" "$BASH" "$SHELL" "$@" > "$BATTY_TEST_ARGUMENTS"\nexit 17\n')
        self.seed = self.base / 'seed'
        self.remote = self.base / 'remote repo.git'
        self.checkout = self.base / 'local bash-os'
        self.env = clean_environment() | {
            'BATTY_BASH_OS_SOURCE': str(self.checkout), 'BATTY_BASH_OS_REMOTE': 'origin',
            'BATTY_BASH_OS_REF': 'HEAD', 'BATTY_OFFLINE': '0',
            'BATTY_TEST_RUNTIME_SOURCE': self.verified_source,
            'BATTY_TEST_ARGUMENTS': str(self.base / 'arguments'),
        }
        self.git('init', '-q', '-b', 'main', str(self.seed))
        self.git('-C', str(self.seed), 'config', 'user.name', 'itsmygithubacct')
        self.git('-C', str(self.seed), 'config', 'user.email', 'itsmygithubacct@users.noreply.github.com')
        (self.seed / 'build.sh').write_text(UPSTREAM_BUILD)
        (self.seed / 'marker').write_text('original')
        self.commit()
        self.git('clone', '-q', '--bare', str(self.seed), str(self.remote))
        self.git('clone', '-q', str(self.remote), str(self.checkout))
        self.runtime = Runtime(self.root, self.env)

    def git(self, *args):
        return subprocess.run(['git', *args], env=self.env, check=True, capture_output=True,
                              text=True, timeout=10).stdout.strip()

    def commit(self):
        self.git('-C', str(self.seed), 'add', '.')
        self.git('-C', str(self.seed), 'commit', '-qm', 'Fixture revision')
        return self.git('-C', str(self.seed), 'rev-parse', 'HEAD')

    def advance(self, marker='remote update', build=None):
        (self.seed / 'marker').write_text(marker)
        if build is not None:
            (self.seed / 'build.sh').write_text(build)
        revision = self.commit()
        self.git('-C', str(self.seed), 'push', '-q', str(self.remote), 'HEAD:main')
        return revision

    def prepare(self, runtime=None):
        runtime = runtime or self.runtime
        with runtime.locked():
            selected = runtime.prepare()
            runtime.ensure_native(selected)
        return selected

    def offline(self):
        return Runtime(self.root, self.env | {'BATTY_OFFLINE': '1'})

    def test_remote_update_preserves_dirty_checkout_and_pairs_headers(self):
        first = self.prepare()
        (self.checkout / 'build.sh').write_text('local work must survive\n')
        (self.checkout / 'untracked').write_text('local data')
        old_head = self.git('-C', str(self.checkout), 'rev-parse', 'HEAD')
        old_status = self.git('-C', str(self.checkout), 'status', '--porcelain')
        revision = self.advance()
        second = self.prepare()
        self.assertNotEqual(first['revision'], second['revision'])
        self.assertEqual(second['revision'], revision)
        self.assertEqual(self.git('-C', str(self.checkout), 'rev-parse', 'HEAD'), old_head)
        self.assertEqual(self.git('-C', str(self.checkout), 'status', '--porcelain'), old_status)
        self.assertEqual((self.checkout / 'build.sh').read_text(), 'local work must survive\n')
        self.assertEqual((Path(second['binary']).parent.parent / 'marker').read_text(), 'remote update')
        self.assertEqual((self.root / 'build/batty.so').read_text(), second['headers'])
        self.prepare()
        self.assertEqual((self.root / 'native-build-count').read_text(), '2')
        self.assertEqual((Path(second['binary']).parent.parent / 'build-count').read_text(), '1')

    def test_fetch_failure_requires_explicit_offline(self):
        first = self.prepare()
        previous = self.runtime.record.read_bytes()
        self.remote.rename(self.base / 'unavailable.git')
        with self.assertRaisesRegex(RuntimeErrorDetail, 'Cannot check the latest'):
            self.prepare()
        self.assertEqual(self.runtime.record.read_bytes(), previous)
        self.assertEqual(self.prepare(self.offline()), first)

    def test_offline_without_verified_pair_fails(self):
        with self.assertRaisesRegex(RuntimeErrorDetail, 'No verified'):
            self.prepare(self.offline())

    def test_failed_remote_build_keeps_previous_pair(self):
        first = self.prepare()
        self.advance(build='exit 23\n')
        with self.assertRaisesRegex(RuntimeErrorDetail, 'exit 23'):
            self.prepare()
        self.assertEqual(json.loads(self.runtime.record.read_text()), first)
        self.assertEqual(self.prepare(self.offline()), first)
        revision = self.advance('fixed', UPSTREAM_BUILD)
        self.assertEqual(self.prepare()['revision'], revision)

    def test_native_build_failure_does_not_publish_new_runtime(self):
        first = self.prepare()
        revision = self.advance()
        (self.root / 'fail-native').touch()
        with self.assertRaisesRegex(RuntimeErrorDetail, 'exit 24'):
            self.prepare()
        self.assertEqual(json.loads(self.runtime.record.read_text()), first)
        (self.root / 'fail-native').unlink()
        self.assertEqual(self.prepare()['revision'], revision)

    def test_modified_binary_rejected_offline_and_rebuilt_online(self):
        first = self.prepare()
        with Path(first['binary']).open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(RuntimeErrorDetail, 'does not match'):
            self.prepare(self.offline())
        repaired = self.prepare()
        self.assertEqual(repaired['sha256'], first['sha256'])
        self.assertEqual((Path(first['binary']).parent.parent / 'build-count').read_text(), '2')

    def test_regular_bash_with_manifest_is_rejected(self):
        selected = self.prepare()
        binary = Path(selected['binary'])
        shutil.copy2('/bin/bash', binary)
        manifest_path = binary.with_name('bash.manifest.json')
        manifest = json.loads(manifest_path.read_text())
        manifest['binary_sha256'] = digest(binary)
        write_json(manifest_path, manifest)
        with self.assertRaises(RuntimeErrorDetail):
            self.runtime.validate(binary.parent.parent)

    def test_exec_uses_bash_os_despite_inherited_bash_and_preserves_arguments(self):
        marker = self.base / 'must-not-exist'
        argument = f'literal $(touch "{marker}") `false` ; spaces'
        command = ['python3', str(self.root / 'tools/runtime.py'), 'launch', '--', argument]
        process = subprocess.run(command, env=self.env | {'BATTY_BASH': '/bin/bash'},
                                 capture_output=True, text=True, timeout=15)
        self.assertEqual(process.returncode, 17, process.stderr)
        selected = json.loads(self.runtime.record.read_text())
        self.assertEqual((self.base / 'arguments').read_text().splitlines(),
                         [selected['binary'], selected['binary'], '--', argument])
        self.assertFalse(marker.exists())

    def test_concurrent_launches_build_once(self):
        command = ['python3', str(self.root / 'tools/runtime.py'), 'launch']
        processes = [subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True) for _ in range(2)]
        try:
            for process in processes:
                _, error = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 17, error)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertEqual((self.root / 'native-build-count').read_text(), '1')
        selected = json.loads(self.runtime.record.read_text())
        self.assertEqual((Path(selected['binary']).parent.parent / 'build-count').read_text(), '1')


if __name__ == '__main__':
    unittest.main(verbosity=2)
