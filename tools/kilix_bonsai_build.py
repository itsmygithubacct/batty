#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Create a relocatable launcher for a pinned Kilix Bonsai checkout."""
from pathlib import Path
import os


def main():
    root = Path.cwd()
    source = root / 'tools/kilix-bonsai/main.py'
    if not source.is_file():
        raise SystemExit('kilix-bonsai source entry point is missing')
    output = root / 'build/kilix-bonsai'
    output.parent.mkdir(exist_ok=True)
    output.write_text('#!/bin/sh\n'
                      'set -eu\n'
                      'root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
                      'exec python3 "$root/tools/kilix-bonsai/main.py" "$@"\n')
    os.chmod(output, 0o700)


if __name__ == '__main__':
    main()
