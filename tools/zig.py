#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Fetch a project-local, checksum-verified compiler when Zig is unavailable."""
import hashlib
import json
from pathlib import Path
import platform
import tarfile
import urllib.request

root = Path(__file__).resolve().parent.parent
lock = json.loads((root / 'deps.lock.json').read_text())['zig']
machine = platform.machine()
target = machine + '-linux'
if platform.system() != 'Linux' or target not in lock:
    raise SystemExit('Install Zig ' + lock['version'] + ' and set ZIG to its executable.')
archive = root / ('.cache/zig-' + target + '.tar.xz')
compiler = root / ('.cache/zig-' + target + '-' + lock['version'] + '/zig')
if not compiler.exists():
    item = lock[target]
    archive.parent.mkdir(exist_ok=True)
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != item['sha256']:
        with urllib.request.urlopen(item['url'], timeout=60) as response, archive.open('wb') as out:
            while data := response.read(1024 * 1024):
                out.write(data)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != item['sha256']:
        raise SystemExit('Zig download checksum mismatch')
    with tarfile.open(archive) as tar:
        tar.extractall(archive.parent, filter='data')
print(compiler)
