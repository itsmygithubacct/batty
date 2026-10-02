#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Apply a bundled desktop Settings choice to an existing Batty frontend."""
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / 'third_party/kilix-desktop/src'
os.environ['KILIX_HOME'] = str(VENDOR)
os.environ['BATTY_KILIX_DESKTOP'] = '1'
os.environ['BATTY_CONTROL'] = sys.argv[1]
os.environ.pop('BATTY_CONTROL_DIR', None)
sys.path[:0] = [str(VENDOR / 'config'), str(VENDOR / 'desktop'), str(ROOT / 'tools')]

import main
from apps import settings as desktop_settings
from control import request
from unittest.mock import patch

desk = main.Desk(term=None)
assert not desk.shell.system_menu_items(), 'bundled desktop exposed checkout update commands'
with patch.object(desk.shell, 'open_catalog_application', return_value=True) as launch:
    assert desk.shell.open_app('kilix-file') is True
    launch.assert_called_once_with('kilix-file', arguments=())
with patch.object(desk.shell, 'open_in_xpane', return_value=True) as launch:
    assert desk.shell.open_catalog_application('kilix-amp') is True
    assert desk.shell.open_catalog_application('kilix-file') is True
    assert desk.shell.open_catalog_application('dosbox') is True
    assert [call.kwargs['fill'] for call in launch.call_args_list] == [False, True, True]
    assert launch.call_args.args[0] == (str(VENDOR / 'kilix'), 'app', 'window', 'dosbox')
window = desktop_settings.SettingsWin(desk)
kitty_before = Path(window.path).read_bytes() if Path(window.path).exists() else None
assert window.batty_mode and window.raw_tab == -1
assert all(spec.source == 'shared' for page in window.page_specs for spec in page)
edge = window.fields['KILIX_CHROME_TAB_BAR_EDGE'][1]
edge.index = edge.options.index(sys.argv[2])
window._apply()
observed = request(sys.argv[1], 'checkpoint')['appearance']['bottom_bar']
assert observed == (sys.argv[2] == 'bottom'), (observed, getattr(window.status, 'text', None),
                                              Path(window.shared_path).read_text())
assert desktop_settings.shared_settings.load(window.shared_path)['KILIX_CHROME_TAB_BAR_EDGE'] == sys.argv[2]
kitty_after = Path(window.path).read_bytes() if Path(window.path).exists() else None
assert kitty_after == kitty_before, 'Batty Settings rewrote kitty.conf'
center = next(item for item in desk.shell.grid.items if item['label'] == 'Pane Center')
desk.shell._activate(center)
deadline = time.monotonic() + 3
while not request(sys.argv[1], 'pane-center-state')['open']:
    assert time.monotonic() < deadline, 'desktop Pane Center did not open in Batty'
    time.sleep(0.03)
mux = next(item for item in desk.shell.grid.items if item['label'] == 'Mux Terminal')
desk.shell._activate(mux)
deadline = time.monotonic() + 8
while not any(pane['session'] == 'kilix-mux-main' for pane in request(sys.argv[1], 'list')['panes']):
    assert time.monotonic() < deadline, 'desktop Mux Terminal did not create a named owner'
    time.sleep(0.04)
print('PASS bundled desktop Settings, Pane Center and Mux Terminal use Batty host actions')
