#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Read supported GPU Terminal presentation and recording settings as data."""
import os
from pathlib import Path
import re
import stat
import sys

LIMIT = 1024 * 1024
ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def settings_path():
    explicit = os.environ.get('GPU_TERMINAL_SETTINGS_FILE')
    if explicit:
        return Path(explicit).expanduser().absolute()
    root = Path(os.environ.get('GPU_TERMINAL_HOME') or '~/.local/gpu_terminal').expanduser().absolute()
    return root / 'settings.conf'


def read_document(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return b''
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Shared settings must be a regular file')
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('Shared settings exceed 1 MiB')
    return data


def read_settings(path):
    values = {}
    for line in read_document(path).decode('utf-8', errors='replace').splitlines():
        match = ASSIGNMENT.match(line)
        if match:
            values[match[1]] = match[2].strip()
    return values


BUTTONS = ('SYNCHRONIZE_INPUT', 'FONT_DECREASE', 'FONT_INCREASE', 'SPLIT_LEFT',
           'SPLIT_UP', 'SPLIT_DOWN', 'SPLIT_RIGHT', 'MAXIMIZE', 'CLOSE')
FALSE = ('', '0', 'no', 'false', 'off', 'disabled')
TRUE = ('1', 'yes', 'true', 'on', 'enabled')
TRANSCRIPTS = {
    'transcript': ('KILIX_TRANSCRIPT', 'on', ('on', 'off')),
    'transcript_size': ('KILIX_TRANSCRIPT_MAX_SIZE', '8M', ('2M', '8M', '32M', '128M')),
    'transcript_graphics': ('KILIX_TRANSCRIPT_GRAPHICS', 'elide', ('elide', 'keep')),
    'transcript_total': ('KILIX_TRANSCRIPT_MAX_TOTAL', '5G', ('1G', '5G', '10G', '20G', '50G', '100G')),
    'transcript_archive_total': ('KILIX_TRANSCRIPT_ARCHIVE_MAX_TOTAL', '1G', ('off', '1G', '5G', '10G', '20G', '50G', '100G')),
}


def transcript_value(values, name):
    key, default, choices = TRANSCRIPTS[name]
    raw = values.get(key, default).lower()
    if name == 'transcript':
        return 'off' if raw in FALSE else 'on'
    return next((choice for choice in choices if choice.lower() == raw), default)


def recording_policy(values):
    enabled = transcript_value(values, 'transcript') == 'on'
    size = transcript_value(values, 'transcript_size')
    return int(enabled), int(size[:-1]) * 1024 ** 2, transcript_value(values, 'transcript_graphics')


def start_menu(values):
    return values.get('KILIX_CHROME_START_MENU', 'off').lower() not in ('', '0', 'no', 'false', 'off', 'disabled')


def pane_buttons(values):
    return sum(1 << i for i, name in enumerate(BUTTONS)
               if values.get('KILIX_CHROME_BUTTON_' + name, 'on').lower()
               not in ('', '0', 'no', 'false', 'off', 'disabled'))


def tab_bar_edge(values=None):
    override = os.environ.get('BATTY_TAB_BAR_EDGE')
    if override:
        if override not in ('top', 'bottom'):
            raise ValueError('BATTY_TAB_BAR_EDGE must be top or bottom')
        return override
    if values is None:
        values = read_settings(settings_path())
    value = values.get('KILIX_CHROME_TAB_BAR_EDGE', '').lower()
    return value if value in ('top', 'bottom') else 'top'


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['--chrome']:
            settings = read_settings(settings_path())
            print(tab_bar_edge(settings), pane_buttons(settings), int(start_menu(settings)))
        elif sys.argv[1:] == ['--recording']:
            print(*recording_policy(read_settings(settings_path())))
        elif sys.argv[1:] == ['--recording-settings']:
            values = read_settings(settings_path())
            print(*(transcript_value(values, name) for name in TRANSCRIPTS))
        elif len(sys.argv) == 1:
            print(tab_bar_edge())
        else:
            raise ValueError('Usage: kilix_settings.py [--chrome | --recording | --recording-settings]')
    except (OSError, ValueError) as error:
        print(f'kilix settings: {error}', file=sys.stderr)
        sys.exit(1)
