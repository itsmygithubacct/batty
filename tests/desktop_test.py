#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise user desktop installation in isolated XDG directories."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import kilix_desktop as desktop


def rejected(call):
    try:
        call()
    except (ValueError, OSError):
        return
    raise AssertionError('Expected unsafe or conflicting installation to fail')


with tempfile.TemporaryDirectory(prefix='batty-desktop-') as temporary:
    base = Path(temporary)
    env = {'XDG_DATA_HOME': str(base / 'data'), 'XDG_STATE_HOME': str(base / 'state')}
    with patch.dict(os.environ, env):
        subprocess.run([str(ROOT / 'kilix'), '--install-desktop'], check=True)
        entry, icon = [base / 'data' / name for name in desktop.FILES]
        record = base / 'state/batty/kilix-desktop.json'
        assert 'Name=Kilix — Batty' in entry.read_text() and 'Icon=batty-kilix' in entry.read_text()
        assert 'StartupWMClass=batty-kilix' in entry.read_text()
        ET.fromstring(icon.read_bytes())
        assert record.stat().st_mode & 0o777 == 0o600
        initial = entry.read_bytes(), icon.read_bytes()
        desktop.update(True)
        assert initial == (entry.read_bytes(), icon.read_bytes())
        icon.write_text('user modification')
        rejected(lambda: desktop.update(True))
        rejected(lambda: desktop.update(False))
        assert entry.read_bytes() == initial[0] and icon.read_text() == 'user modification'
        icon.write_bytes(initial[1])
        unrelated = entry.parent / 'kilix.desktop'
        unrelated.write_text('reference Kilix installation')
        subprocess.run([str(ROOT / 'kilix'), '--uninstall-desktop'], check=True)
        assert not entry.exists() and not icon.exists() and not record.exists()
        assert unrelated.read_text() == 'reference Kilix installation'
        desktop.update(False)
        entry.write_text('unowned entry')
        rejected(lambda: desktop.update(True))
        assert entry.read_text() == 'unowned entry'
        entry.unlink()
        entry.symlink_to(unrelated)
        rejected(lambda: desktop.update(True))
        assert unrelated.read_text() == 'reference Kilix installation'
        entry.unlink()
        # Interrupted multi-file updates retain a recoverable ownership journal.
        original_write = desktop.atomic_write
        def fail_icon(path, data, mode=0o644):
            if path == icon:
                raise OSError('injected icon publication failure')
            original_write(path, data, mode)
        with patch.object(desktop, 'atomic_write', fail_icon):
            rejected(lambda: desktop.update(True))
        assert entry.exists() and record.exists() and not icon.exists()
        desktop.update(True)
        assert icon.exists()
        with patch.dict(os.environ, XDG_DATA_HOME=str(base / 'different')):
            rejected(lambda: desktop.update(True))
            desktop.update(False)
        assert not entry.exists() and not icon.exists()
        record.write_text('[]')
        rejected(lambda: desktop.update(False))
        record.unlink()
        with patch.dict(os.environ, XDG_DATA_HOME='relative'):
            rejected(lambda: desktop.update(True))
        # Real desktop execution must keep metacharacters literal. The fake
        # checkout writes a marker without launching a terminal or modifying it.
        checkout = base / 'space " quote $ ` back\\slash % equals= café'
        (checkout / 'assets').mkdir(parents=True)
        shutil.copyfile(ROOT / 'assets/batty-kilix.svg', checkout / 'assets/batty-kilix.svg')
        (checkout / 'kilix').write_text('#!/bin/sh\nprintf launched > "$BATTY_TEST_LAUNCHED"\n')
        desktop.update(True, checkout)
        probe = subprocess.run(['/usr/bin/python3', '-c', 'from gi.repository import Gio'], capture_output=True)
        if probe.returncode == 0:
            marker = base / 'launched'
            subprocess.run(['/usr/bin/python3', '-c',
                            'from gi.repository import Gio; import sys; '
                            'app=Gio.DesktopAppInfo.new_from_filename(sys.argv[1]); '
                            'assert app is not None; assert app.launch([], None)', str(entry)],
                           env=os.environ | {'BATTY_TEST_LAUNCHED': str(marker)}, check=True)
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.read_text() == 'launched'
        else:
            print('SKIP optional GLib desktop launch probe: Python GObject bindings unavailable')
        desktop.update(True, ROOT)
        assert desktop.exec_argument(str(ROOT / 'kilix')) in entry.read_text()
        desktop.update(False)
        assert not entry.exists() and not icon.exists()
print('PASS desktop integration: install, update, ownership conflicts, interrupted recovery, uninstall and literal launch paths')
