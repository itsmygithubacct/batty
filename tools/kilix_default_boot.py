#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Open the selected desktop, then keep the initial terminal page usable."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ('95', 'xp', 'tui', 'cap', 'land', 'icewm'):
        raise ValueError('default desktop must be 95, xp, tui, cap, land, or icewm')
    result = subprocess.run([str(ROOT / 'kilix'), 'desktop', sys.argv[1]], check=False)
    if result.returncode:
        print(f'kilix desktop: startup failed ({result.returncode})', file=sys.stderr)
    shell = sys.argv[2]
    os.execv(shell, [shell])


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print(f'kilix desktop: {error}', file=sys.stderr)
        raise SystemExit(1)
