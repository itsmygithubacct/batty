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
if platform.system() != 'Linux' or platform.machine() != 'x86_64':
    raise SystemExit('Install Zig ' + lock['version'] + ' and set ZIG to its executable.')
archive = root / '.cache/zig.tar.xz'
compiler = root / ('.cache/zig-x86_64-linux-' + lock['version'] + '/zig')
if not compiler.exists():
    item = lock['x86_64-linux']
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
