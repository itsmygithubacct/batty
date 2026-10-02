#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Prepare immutable browser assets for Kilix's network application provider."""
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from kilix_apps import private_directory

NOVNC_REPOSITORY = 'https://github.com/novnc/noVNC.git'
NOVNC_REF = '7fcf9dcfe0cc5b14e3841a4429dc091a6ffca861'
FILES = {
    'hlsjs/hls.min.js': (
        'https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js',
        '484054e8cd03d3f6d1781fb7f402bdc318d8a4c527f933a95c624e27cc9a9470'),
    'mpegtsjs/mpegts.js': (
        'https://cdn.jsdelivr.net/npm/mpegts.js@1.7.3/dist/mpegts.js',
        'b83dccba1525ae65763b373a85a6513f0b533359292a90083b25b29977a75f90'),
}


def asset_directory(path):
    """Tighten old provider-created directories inside Batty's private root."""
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError(f'unsafe web asset directory: {path}')
    if info.st_mode & 0o077:
        path.chmod(stat.S_IMODE(info.st_mode) & ~0o077)
    return Path(private_directory(path))


def run(*argv, timeout=120):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f'web asset command timed out: {argv[0]}') from error
    if result.returncode:
        raise RuntimeError(f'web asset command failed: {argv[0]}: '
                           f'{(result.stderr or result.stdout).strip()[-500:]}')
    return result.stdout.strip()


def digest(path):
    if path.is_symlink() or not path.is_file():
        return None
    result = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def novnc_ready(directory):
    if not directory.is_dir() or directory.is_symlink() or not (directory / 'vnc.html').is_file():
        return False
    try:
        return (run('git', '-C', str(directory), 'rev-parse', 'HEAD') == NOVNC_REF
                and run('git', '-C', str(directory), 'remote', 'get-url', 'origin') == NOVNC_REPOSITORY
                and not run('git', '-C', str(directory), 'status', '--porcelain'))
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return False


def prepare_novnc(data):
    destination = data / 'novnc'
    if novnc_ready(destination):
        return
    if destination.exists() or destination.is_symlink():
        raise RuntimeError(f'unverified noVNC assets at {destination}')
    with tempfile.TemporaryDirectory(prefix='.novnc-', dir=data) as scratch:
        staged = Path(scratch) / 'source'
        run('git', 'init', '--quiet', str(staged))
        run('git', '-C', str(staged), 'remote', 'add', 'origin', NOVNC_REPOSITORY)
        run('git', '-C', str(staged), 'fetch', '--quiet', '--depth', '1', 'origin', NOVNC_REF,
            timeout=300)
        run('git', '-C', str(staged), 'checkout', '--quiet', '--detach', 'FETCH_HEAD')
        if not novnc_ready(staged):
            raise RuntimeError('downloaded noVNC tree differs from its pinned commit')
        staged.rename(destination)


def prepare_file(data, name):
    url, expected = FILES[name]
    target = data / name
    parent = asset_directory(target.parent)
    if digest(target) == expected:
        return
    with tempfile.TemporaryDirectory(prefix='.web-', dir=parent) as scratch:
        staged = Path(scratch) / target.name
        run('curl', '--fail', '--location', '--silent', '--show-error',
            '--proto', '=https', '--tlsv1.2', '--max-time', '120',
            '--max-filesize', str(10 * 1024 * 1024), '--output', str(staged), url,
            timeout=140)
        if digest(staged) != expected:
            raise RuntimeError(f'web asset SHA-256 differs: {name}')
        os.replace(staged, target)


def prepare(*, mse=False):
    data = asset_directory(os.environ['KILIX_DATA_HOME'])
    lock = os.open(data / '.web-assets.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock, 'rb') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        prepare_novnc(data)
        prepare_file(data, 'hlsjs/hls.min.js')
        if mse:
            prepare_file(data, 'mpegtsjs/mpegts.js')
