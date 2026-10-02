#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A real private-display X application used by the provider integration test."""
import json
import os
from pathlib import Path
import select
import sys
from Xlib import X, display

root = Path(os.environ['BATTY_PROVIDER_TEST'])
if len(sys.argv) > 2:
    root /= sys.argv[2]
    root.mkdir(exist_ok=True)


def save(name, value):
    temporary = root / (name + '.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(root / name)

connection = display.Display()
screen = connection.screen()
window = screen.root.create_window(0, 0, 320, 240, 0, screen.root_depth,
    X.InputOutput, X.CopyFromParent, background_pixel=0x224466,
    event_mask=X.KeyPressMask | X.KeyReleaseMask | X.ButtonPressMask | X.ButtonReleaseMask | X.StructureNotifyMask)
window.set_wm_name('Batty provider test')
window.map()
connection.sync()
geometry = window.get_geometry()
save('geometry.json', {'width': geometry.width, 'height': geometry.height})
save('app.json', {'pid': os.getpid(), 'display': os.environ['DISPLAY'],
    'storage': os.environ['KILIX_STORAGE_HOME'], 'argv': sys.argv[1:],
    'x11_class': os.environ.get('SDL_VIDEO_X11_WMCLASS'),
    'wayland_class': os.environ.get('SDL_VIDEO_WAYLAND_WMCLASS')})
with (root / 'events.jsonl').open('w') as output:
    while not (root / 'quit').exists():
        if not connection.pending_events() and not select.select([connection.fileno()], [], [], 0.05)[0]:
            continue
        event = connection.next_event()
        if event.type == X.ConfigureNotify:
            save('geometry.json', {'width': event.width, 'height': event.height})
        if event.type in (X.KeyPress, X.KeyRelease, X.ButtonPress, X.ButtonRelease):
            output.write(json.dumps({'type': event.type, 'detail': event.detail}) + '\n')
            output.flush()
        if event.type == X.ButtonRelease:
            window.change_attributes(background_pixel=0x22aa44)
            window.clear_area()
            connection.flush()
connection.close()
