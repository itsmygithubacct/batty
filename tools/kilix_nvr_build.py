#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build pinned kilix-nvr with Batty's verified source-initialization fix.

The pinned checkout must remain clean for kilix-content's source verification.
Patch only during compilation, then restore its original bytes and timestamp.
The ignored build marker binds the fix version to the produced executable.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

SOURCE = Path('third_party/kilix-rtsp/src/krtsp_source.c')
BINARY = Path('build/kilix-nvr')
MARKER = Path('build/.batty-nvr-seek.json')
SOURCE_SHA256 = '27e95d5b249e67ddafb1df43c345f0b7898dc53f7337478acec86697a31abff1'
FIX_VERSION = 1
OLD = b'    options->segment_stall_ms = 0;\n}'
NEW = b'    options->segment_stall_ms = 0;\n    options->seek_seconds = 0;\n}'


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def ready(directory):
    root = Path(directory)
    source, binary, marker = root / SOURCE, root / BINARY, root / MARKER
    if not source.is_file() or not binary.is_file() or not marker.is_file():
        return False
    try:
        if sha256(source) != SOURCE_SHA256:
            return False
        stamp = json.loads(marker.read_text())
        binary_hash = sha256(binary)
    except (OSError, ValueError):
        return False
    if not isinstance(stamp, dict):
        return False
    return (stamp.get('version') == FIX_VERSION
            and stamp.get('source_sha256') == SOURCE_SHA256
            and stamp.get('binary_sha256') == binary_hash)


def build(directory):
    root = Path(directory)
    source, binary, marker = root / SOURCE, root / BINARY, root / MARKER
    original_stat = source.stat()
    original = source.read_bytes()
    if hashlib.sha256(original).hexdigest() != SOURCE_SHA256 or original.count(OLD) != 1:
        raise RuntimeError('kilix-nvr source differs from the reviewed pin')
    source.write_bytes(original.replace(OLD, NEW))
    try:
        result = subprocess.run(['make', 'all'], cwd=root, check=False)
    finally:
        source.write_bytes(original)
        os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    if result.returncode:
        raise RuntimeError(f'kilix-nvr build failed with status {result.returncode}')
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError('kilix-nvr build produced no executable')
    stamp = {'version': FIX_VERSION, 'source_sha256': SOURCE_SHA256,
             'binary_sha256': sha256(binary)}
    temporary = marker.with_name(marker.name + f'.{os.getpid()}.tmp')
    try:
        temporary.write_text(json.dumps(stamp, sort_keys=True) + '\n')
        temporary.chmod(0o600)
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        build(Path.cwd())
    except (OSError, RuntimeError) as error:
        print(f'kilix-nvr build: {error}', file=sys.stderr)
        raise SystemExit(1)
