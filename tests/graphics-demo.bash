#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Requires a built Batty, Bash 5.3 and an external DISPLAY (Xvfb or a desktop).
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$root"
rm -f -- build/graphics-demo-results.json build/graphics-demo.png
enable -f "$root/build/batty.so" batty
terminal=''
cleanup() {
    local result=$?
    trap - EXIT
    [[ -z $terminal ]] || batty close "$terminal" || :
    return "$result"
}
trap cleanup EXIT
batty new -h terminal -W 120 -H 40 --font monospace --font-size 16 -- \
    "$root/tools/graphics-demo.py" --protocol both --width 480 --height 300 --seconds 15
batty graphics "$terminal" >build/graphics-demo-gpu.txt
deadline=$((SECONDS+10))
ready=false
screen=''
while ((SECONDS<deadline)); do
    if ! batty pump "$terminal" -t 10; then
        printf '%s\n' 'FAIL: graphics demo exited before displaying both panels' >&2
        exit 1
    fi
    batty dump "$terminal" >build/graphics-demo-screen.txt
    screen=$(<build/graphics-demo-screen.txt)
    if [[ $screen == *'Kitty: OK | Sixel: advertised'* && $screen == *'KITTY / '* &&
          $screen == *'SIXEL / '* && $screen == *'Q quit'* ]]; then
        ready=true
        break
    fi
done
if ! "$ready"; then
    printf '%s\n' 'FAIL: the child did not receive capability replies and finish its graphics frame' "$screen" >&2
    exit 1
fi
batty capture "$terminal" build/graphics-demo.ppm
batty info "$terminal" >build/graphics-demo-session.txt
batty send "$terminal" q
deadline=$((SECONDS+5))
result=running
while [[ $result == running ]] && ((SECONDS<deadline)); do
    if batty pump "$terminal" -t 10; then :; else [[ $? == 1 ]]; fi
    batty status "$terminal" -V result
done
[[ $result == 0 ]] || { printf 'FAIL: graphics demo exit status: %s\n' "$result" >&2; exit 1; }
batty close "$terminal"
terminal=''
python3 - <<'PY'
import json
from pathlib import Path
import re

root = Path('build')
screen = (root / 'graphics-demo-screen.txt').read_text()
assert 'Kitty: OK | Sixel: advertised' in screen, 'capability replies did not reach the child'
metrics = re.search(r'Cell (\d+) x (\d+) px', screen)
assert metrics, 'the demo did not report terminal pixel geometry'
cw, ch = map(int, metrics.groups())
assert '(estimated)' not in screen, 'Batty did not supply actual cell pixel geometry'
with (root / 'graphics-demo.ppm').open('rb') as frame:
    assert frame.readline() == b'P6\n'
    width, height = map(int, frame.readline().split())
    assert frame.readline() == b'255\n'
    pixels = frame.read()
assert len(pixels) == width * height * 3, 'incomplete framebuffer capture'

def pixel(x, y):
    assert 0 <= x < width and 0 <= y < height, 'panel pixel lies outside the framebuffer'
    offset = (y * width + x) * 3
    return tuple(pixels[offset:offset + 3])

panels = {}
for row, line in enumerate(screen.splitlines()):
    for match in re.finditer(r'(KITTY|SIXEL) / (\d+) x (\d+) pixels', line):
        protocol, iw, ih = match.groups()
        iw, ih = int(iw), int(ih)
        # Labels start at the image's column, one row above its native pixels.
        origin_x, origin_y = 8 + match.start() * cw, 8 + (row + 1) * ch
        assert iw >= 200 and ih >= 125, 'test window is too small for pixel-detail checks'
        samples = 0
        for y in range(int(ih * 0.17), int(ih * 0.17) + 12):
            for x in range(int(iw * 0.17), int(iw * 0.17) + 12):
                if x % 40 == 0 or y % 40 == 0:
                    continue  # The procedural pattern draws grid lines here.
                expected = (245 if (x + y) % 2 else 16) if protocol == 'KITTY' else (255 if (x + y) % 2 else 0)
                actual = pixel(origin_x + x, origin_y + y)
                # Permit sampler precision differences while requiring alternating
                # native pixels; a solid rectangle or character grid cannot pass.
                assert max(abs(channel - expected) for channel in actual) <= 20, \
                    f'{protocol}: missing native 1px detail at {x},{y}: {actual}, expected {expected}'
                samples += 1
        assert samples >= 100
        colors = {pixel(origin_x + x, origin_y + y)
                  for y in range(3, int(ih * 0.64), 3)
                  for x in range(3, iw - 3, 3)}
        assert len(colors) > (500 if protocol == 'KITTY' else 40), f'{protocol}: missing color raster'
        panels[protocol.lower()] = dict(width=iw, height=ih, checker_samples=samples, colors=len(colors))
assert set(panels) == {'kitty', 'sixel'}, 'both native image panels must be present'
gpu = (root / 'graphics-demo-gpu.txt').read_text()
assert 'GL_VERSION: OpenGL ES' in gpu, 'test did not use the GLES renderer'
result = dict(passed=True, capability_replies=True, child_exit=0,
              framebuffer=dict(width=width, height=height), cell_pixels=[cw, ch], panels=panels)
(root / 'graphics-demo-results.json').write_text(json.dumps(result, indent=2) + '\n')
try:
    from PIL import Image
except ImportError:
    pass
else:
    Image.frombytes('RGB', (width, height), pixels).save(root / 'graphics-demo.png')
print('PASS graphics demo PTY upload, capability replies, Kitty/Sixel native 1px textures and clean exit')
print(json.dumps(result, sort_keys=True))
PY
