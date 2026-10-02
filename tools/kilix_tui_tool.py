#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run a pinned TUI utility with Batty-only source discovery."""
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parent.parent


def main():
    if len(sys.argv) < 3:
        raise ValueError('missing pinned TUI utility')
    checkout = Path(sys.argv[1]).resolve(strict=True)
    target = (checkout / sys.argv[2]).resolve(strict=True)
    if not target.is_relative_to(checkout) or not target.is_file():
        raise ValueError('TUI utility is outside its pinned checkout')
    sys.path.insert(0, str(checkout / 'src'))
    from kilix_desk import sources
    sources.candidates = lambda: (str(ROOT),)
    sys.argv = [str(target), *sys.argv[3:]]
    runpy.run_path(str(target), run_name='__main__')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print(f'kilix tui utility: {error}', file=sys.stderr)
        raise SystemExit(1)
