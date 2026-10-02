#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check XDG discovery precedence and literal provider/terminal launch routing."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from kilix_catalog import launch_command
from desktop_exec import expand_exec
from kilix_desktop import exec_argument

with tempfile.TemporaryDirectory(prefix='bt-catalog-') as directory:
    root = Path(directory)
    user = root / 'user/applications'
    system = root / 'system/applications'
    user.mkdir(parents=True)
    system.mkdir(parents=True)
    env = os.environ | {'XDG_DATA_HOME': str(user.parent),
                        'XDG_DATA_DIRS': str(system.parent), 'LC_ALL': 'C'}

    def desktop(base, name, values):
        (base / name).write_text('[Desktop Entry]\nType=Application\n' + values)

    desktop(system, 'hidden.desktop', 'Name=Hidden\nExec=/bin/true\n')
    desktop(user, 'hidden.desktop', 'Hidden=true\n')
    desktop(system, 'editor.desktop', 'Name=System editor\nExec=/bin/false\n')
    desktop(user, 'editor.desktop', 'Name=User editor\nExec=/bin/echo "a  b" "$(false);literal" %% %U\n'
            'Categories=Development;\nTerminal=true\nPath=' + str(root) + '\n')
    desktop(user, 'gui.desktop', 'Name=Graphics\nExec=/bin/true\nCategories=Graphics;\n')
    desktop(user, 'absent.desktop', 'Name=Absent\nTryExec=batty-nonexistent-catalog-test\nExec=/bin/true\n')
    desktop(user, 'private.desktop', 'Name=Private\nNoDisplay=true\nExec=/bin/true\n')
    os.mkfifo(user / 'pipe.desktop')

    def cli(*args):
        return subprocess.run([str(ROOT / 'kilix'), 'apps', *args], env=env,
                              capture_output=True, text=True, timeout=5)

    result = cli('list', '--json')
    assert result.returncode == 0, result.stderr
    catalog = {e['id']: e for e in json.loads(result.stdout)}
    assert set(catalog) == {'editor.desktop', 'gui.desktop'}, catalog
    editor = catalog['editor.desktop']
    assert editor['name'] == 'User editor' and editor['category'] == 'Development'
    literal = ['/bin/echo', 'a  b', '$(false);literal', '%']
    launcher = str(ROOT / 'kilix')
    command, cwd = launch_command(editor, False)
    assert command == [launcher, '--', *literal] and cwd == str(root)
    command, cwd = launch_command(editor, True)
    assert command == [launcher, 'new-page', '--cwd', str(root), '--', *literal]
    assert launch_command(editor, True, True) == (literal, str(root))
    menu = cli('_menu')
    assert menu.returncode == 0 and menu.stdout.endswith('DONE\0')
    assert 'User editor\0editor.desktop\0' in menu.stdout
    gui = catalog['gui.desktop']
    command, cwd = launch_command(gui, False)
    assert command == [launcher, 'run', '--', '/bin/true']
    command, cwd = launch_command(gui, True)
    assert command == [launcher, 'new-page', '--cwd', cwd, '--', launcher, 'run', '--', '/bin/true']
    for invalid in (editor | {'exec_raw': '"'}, editor | {'exec_raw': ''},
                    editor | {'workdir': str(root / 'missing')}, editor | {'workdir': 'relative'}):
        try:
            launch_command(invalid, False)
        except ValueError:
            pass
        else:
            raise AssertionError('Invalid launcher was accepted')
    assert cli('open', 'hidden.desktop').returncode == 1
    assert cli('open', '../editor.desktop').returncode == 1
    assert cli('list').returncode == 0

    entry = editor | {'icon': 'editor-icon', 'path': str(user / 'editor.desktop')}
    documents = [str(root / 'document with spaces.txt'), str(root / '$(false);%c.txt')]
    assert expand_exec('/bin/echo %F %i %c %k %%', entry, documents) == [
        '/bin/echo', *documents, '--icon', 'editor-icon', 'User editor', entry['path'], '%']
    assert expand_exec('/bin/echo %F %i', entry | {'icon': ''}) == ['/bin/echo']
    assert expand_exec('/bin/echo %U', entry, documents) == ['/bin/echo', *[Path(p).as_uri() for p in documents]]
    url = 'https://example.invalid/a%20b?q=%c'
    assert expand_exec('/bin/echo %u', entry, [url]) == ['/bin/echo', url]
    assert expand_exec('/bin/echo %f', entry, [Path(documents[0]).as_uri()]) == ['/bin/echo', documents[0]]
    assert expand_exec('/bin/echo "" %d %D %n %N %v %m', entry) == ['/bin/echo', '']
    # Desktop installation and catalog launching agree on two-level escaping.
    unusual = str(root / 'quote " dollar $ backtick ` slash\\ percent % café')
    from kilix_catalog import xdgapps
    raw = '/bin/sh ' + exec_argument(unusual)
    assert expand_exec(xdgapps.unescape(raw), entry) == ['/bin/sh', unusual]
    for value, inputs in (('/bin/echo %Z', []), ('/bin/echo %', []),
                          ('/bin/echo "%F"', documents), ('/bin/echo prefix%F', documents),
                          ('/bin/echo %f %u', []), ('/bin/echo %f', documents),
                          ('/bin/echo %f', [url]), ('/bin/echo', documents),
                          ('/bin/echo "unterminated', [])):
        try:
            expand_exec(value, entry, inputs)
        except ValueError:
            pass
        else:
            raise AssertionError(('Invalid field expansion accepted', value))
    capture = root / 'arguments.json'
    writer = root / 'record.py'
    writer.write_text('import json,sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n')
    desktop(user, 'documents.desktop', 'Name=Document recorder\nTerminal=true\nExec="' + sys.executable +
            '" "' + str(writer) + '" "' + str(capture) + '" %F %c %i\nIcon=fixture-icon\n')
    launched = cli('open', '--in-place', 'documents.desktop', '--', *documents)
    assert launched.returncode == 0, launched.stderr
    assert json.loads(capture.read_text()) == [*documents, 'Document recorder', '--icon', 'fixture-icon']

print('PASS installed application discovery, precedence, hidden entries, literal field expansion, document launch and routing')
