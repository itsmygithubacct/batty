#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Feed live process and agent labels to the native Pane Center while open."""
import os
import time

from control import request
from kilix_agent import coding_session
from kilix_panes import activity, plain, process_info


def sample(path, ids):
    panes = {pane['id']: pane for pane in request(path, 'list')['panes']}
    for pane_id in ids[:64]:
        pane = panes.get(pane_id)
        if pane is None:
            continue
        process, cwd, foreground = process_info(pane)
        coding, coding_activity = coding_session(foreground, cwd)
        state = activity(pane, process, coding_activity)
        name = plain(process['name'])[:64]
        agent = ''
        if coding:
            agent = coding['provider']
            if coding['session_id'] != 'unknown':
                agent += ' ' + coding['session_id'][:8]
        task = (plain(coding['last_user_message'] or coding['title']).encode('utf-8')[:160].decode('utf-8', 'ignore')
                if coding else '')
        payload = b'\0'.join(part.encode('utf-8') for part in (state, name, agent, task))
        request(path, 'telemetry', pane_id, payload)


def main():
    parent = os.getppid()
    path = os.environ['BATTY_CONTROL']
    while os.getppid() == parent:
        try:
            center = request(path, 'pane-center-state', timeout=2)
            if center['open']:
                sample(path, center['ids'])
                time.sleep(1)
            else:
                time.sleep(0.25)
        except (OSError, RuntimeError, KeyError, TypeError, ValueError):
            if not os.path.exists(path):
                break
            time.sleep(0.5)


if __name__ == '__main__':
    main()
