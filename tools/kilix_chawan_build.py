#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build pinned Chawan with a checksum-pinned, private Nim toolchain."""
import hashlib
import os
from pathlib import Path
import platform
import subprocess
import tarfile
import tempfile

from kilix_content_app import apps_root

NIM_VERSION = '2.2.10'
NIM_ARCHIVES = {
    'x86_64': ('linux_x64', '0a3a38752e97e9d44aa479b3a7b37336dfe0176daf22ee5b5218ad0991ecd211'),
    'aarch64': ('linux_arm64', 'cd86a6e2bcbf029c4870aa51df5c0169345dbf9959889112fd15d403c13ae33a'),
    'i686': ('linux_x32', '7e018e66e570943c8e079e5cf78898444fc627bc0d47b7a5c17dc97cbc12083e'),
}


def nim_ready(path):
    if not path.is_file() or path.is_symlink() or not os.access(path, os.X_OK):
        return False
    result = subprocess.run([str(path), '--version'], capture_output=True,
                            text=True, timeout=10)
    return result.returncode == 0 and result.stdout.startswith(
        f'Nim Compiler Version {NIM_VERSION} ')


def ensure_nim():
    toolchains = Path(apps_root()).parent.parent / 'toolchains'
    toolchains.mkdir(mode=0o700, exist_ok=True)
    if toolchains.is_symlink():
        raise RuntimeError(f'unsafe Nim toolchain directory: {toolchains}')
    home = toolchains / f'nim-{NIM_VERSION}'
    binary = home / 'bin/nim'
    if nim_ready(binary):
        return binary
    if home.exists() or home.is_symlink():
        raise RuntimeError(f'invalid pinned Nim toolchain at {home}')
    arch = {'amd64': 'x86_64', 'arm64': 'aarch64', 'i386': 'i686'}.get(
        platform.machine().lower(), platform.machine().lower())
    if arch not in NIM_ARCHIVES:
        raise RuntimeError(f'no pinned Nim archive for {arch}')
    label, expected = NIM_ARCHIVES[arch]
    url = f'https://nim-lang.org/download/nim-{NIM_VERSION}-{label}.tar.xz'
    with tempfile.TemporaryDirectory(prefix='.nim-', dir=toolchains) as scratch:
        archive = Path(scratch) / 'nim.tar.xz'
        subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error',
                        '--proto', '=https', '--tlsv1.2', '--max-time', '600',
                        '--max-filesize', str(200 * 1024 * 1024), '--output',
                        str(archive), url], check=True, timeout=620)
        digest = hashlib.sha256()
        with archive.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise RuntimeError('pinned Nim archive SHA-256 differs')
        with tarfile.open(archive, 'r:xz') as bundle:
            bundle.extractall(scratch, filter='data')
        unpacked = Path(scratch) / f'nim-{NIM_VERSION}'
        if not nim_ready(unpacked / 'bin/nim'):
            raise RuntimeError('pinned Nim archive has no working compiler')
        unpacked.rename(home)
    return binary


def main():
    os.umask(0o077)
    source = Path.cwd()
    if not (source / 'Makefile').is_file():
        raise RuntimeError('Chawan Makefile is missing')
    for package in ('libssl', 'libcrypto', 'libbrotlidec', 'libbrotlicommon'):
        subprocess.run(['pkg-config', '--exists', package], check=True)
    nim = ensure_nim()
    sftp = subprocess.run(['pkg-config', '--exists', 'libssh2'],
                          check=False).returncode == 0
    environment = os.environ | {'PATH': str(nim.parent) + os.pathsep + os.environ['PATH']}
    subprocess.run(['make', '--no-print-directory', '-j4', f'NIM={nim}',
                    f'CHA_SFTP={int(sftp)}'], cwd=source, env=environment,
                   check=True, timeout=3300)
    binary = source / 'target/release/bin/cha'
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError('Chawan build produced no runnable browser')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError, tarfile.TarError) as error:
        raise SystemExit(f'kilix-chawan build: {error}') from error
