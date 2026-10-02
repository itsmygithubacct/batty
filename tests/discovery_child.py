#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / 'tools'))
from control_paths import resolve_endpoint

for line in sys.stdin:
    sequence = int(line.strip())
    report = {'endpoint': resolve_endpoint(), 'pid': os.getpid(), 'inherited': os.environ['BATTY_CONTROL']}
    for tool, args in (('kilix', ['ls', '--json']), ('battyctl', ['list'])):
        p = subprocess.run([str(root / tool), *args], capture_output=True, text=True, timeout=5)
        if p.returncode:
            raise RuntimeError(p.stderr)
        report[tool] = json.loads(p.stdout)
    Path(sys.argv[1], f'report-{sequence}.json').write_text(json.dumps(report))
