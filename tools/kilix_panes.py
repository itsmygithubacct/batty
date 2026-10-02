#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pane Center commands for the in-project Batty Kilix frontend."""
import argparse
from collections import deque
import json
import os
from pathlib import Path
import sys
import time

from control import request
from control_paths import ancestors, resolve_endpoint
from kilix_agent import coding_session

SHELLS = frozenset(('ash', 'bash', 'dash', 'fish', 'ksh', 'nu', 'sh', 'tcsh', 'zsh'))
PROCESS_LIMIT = 64


def plain(value):
    return ''.join(c if c.isprintable() else ' ' for c in str(value or '')).strip()


def process_stat(pid):
    """Read only the fields needed to match one PTY session and foreground group."""
    try:
        raw = (Path('/proc') / str(pid) / 'stat').read_text()
        end = raw.rfind(')')
        if end < 0:
            return None
        fields = raw[end + 2:].split()
        if len(fields) < 6:
            return None
        return {'pid': pid, 'ppid': int(fields[1]), 'pgrp': int(fields[2]),
                'session': int(fields[3]), 'tty': int(fields[4]), 'tpgid': int(fields[5])}
    except (OSError, ValueError):
        return None


def process_record(pid):
    path = Path('/proc') / str(pid)
    record = {'pid': pid, 'name': None, 'argv': [], 'cwd': None}
    try:
        with (path / 'cmdline').open('rb') as stream:
            raw = stream.read(4096)
        record['argv'] = [part.decode('utf-8', 'replace') for part in raw.split(b'\0') if part][:32]
        record['name'] = (path / 'comm').read_text().strip()[:64] or None
    except OSError:
        pass
    try:
        record['cwd'] = os.readlink(path / 'cwd')
    except OSError:
        pass
    return record


def proc_children():
    children = {}
    try:
        entries = list(Path('/proc').iterdir())
    except OSError:
        return children
    for entry in entries:
        if entry.name.isdecimal():
            stat = process_stat(int(entry.name))
            if stat:
                children.setdefault(stat['ppid'], []).append(stat['pid'])
    return children


def child_pids(pid, fallback):
    try:
        with (Path('/proc') / str(pid) / 'task' / str(pid) / 'children').open('r') as stream:
            raw = stream.read(4096)
        return [int(value) for value in raw.split()[:PROCESS_LIMIT]]
    except FileNotFoundError:
        if 'children' not in fallback:
            fallback['children'] = proc_children()
        return fallback['children'].get(pid, [])[:PROCESS_LIMIT]
    except (OSError, ValueError):
        return []


def process_info(pane, fallback=None):
    if fallback is None:
        fallback = {}
    pid = pane.get('pid')
    result = {'name': None, 'argv': [], 'child_pid': pid if isinstance(pid, int) and pid > 0 else None,
              'foreground': []}
    if not result['child_pid'] or pane.get('exit_status') is not None:
        return result, None, []
    root = process_stat(pid)
    initial = process_record(pid)
    foreground = []
    if root and root['tty'] and root['tpgid'] > 0:
        pending = deque([(pid, 0)]); seen = set(); attempts = 0
        while pending and len(seen) < PROCESS_LIMIT and attempts < PROCESS_LIMIT * 2:
            current, parent = pending.popleft()
            attempts += 1
            if current in seen:
                continue
            stat = process_stat(current)
            if not stat or (parent and stat['ppid'] != parent) or stat['session'] != root['session']:
                continue
            seen.add(current)
            if stat['tty'] == root['tty'] and stat['pgrp'] == root['tpgid']:
                foreground.append(process_record(current))
            pending.extend((child, current) for child in child_pids(current, fallback) if child not in seen)
    representative = foreground[-1] if foreground else initial
    result['name'] = representative['name']
    result['argv'] = representative['argv']
    result['foreground'] = [{'pid': item['pid'], 'argv': item['argv'], 'cwd': item['cwd']}
                            for item in foreground]
    return result, representative['cwd'] or initial['cwd'], foreground or [initial]


def activity(pane, process, coding_activity):
    if pane['exit_status'] is not None:
        return 'exited'
    if pane.get('disconnected'):
        return 'disconnected'
    if coding_activity:
        return coding_activity
    name = (process['name'] or '').casefold()
    if name in SHELLS:
        return 'shell'
    if name in ('ssh', 'mosh-client'):
        return 'remote'
    return 'running' if name else 'unknown'


def pane_snapshot(path):
    listed = request(path, 'list')['panes']
    panes = []
    fallback = {}
    for pane in listed:
        try:
            details = request(path, 'info', pane['id'])['panes'][0]
        except (IndexError, KeyError, RuntimeError) as error:
            if 'Pane does not exist' in str(error) or isinstance(error, (IndexError, KeyError)):
                continue  # Pane closed between the two bounded control reads.
            raise
        pane.update(details)
        panes.append(pane)
    page_ids = list(dict.fromkeys(p['tab'] for p in panes))
    lineage = set(ancestors())
    own = next((p['id'] for p in panes if p['pid'] in lineage and
                p['exit_status'] is None and not p['observe']), None)
    records = []
    for pane in panes:
        process, cwd, foreground_records = process_info(pane, fallback)
        coding, coding_activity = coding_session(foreground_records, cwd)
        session = pane.get('session') if pane.get('persistent') else None
        records.append({
            'pane_id': pane['id'],
            'page': {'id': pane['tab'], 'index': pane.get('page_index', page_ids.index(pane['tab']) + 1),
                     'title': pane.get('page_title') or pane.get('title') or '',
                     'os_window_id': pane['window']},
            'focused': pane['active'], 'title': pane.get('title') or '', 'cwd': cwd,
            'activity': activity(pane, process, coding_activity),
            'doing': (plain(coding['last_user_message'] or coding['title'])[:160] if coding and
                      (coding['last_user_message'] or coding['title']) else
                      plain(pane.get('title'))[:160] if pane.get('title') and
                      pane['title'].casefold() != (process['name'] or '').casefold() else None),
            'process': process, 'coding_session': coding,
            'broker': None,
            'batty_session': ({'name': session, 'directory': pane.get('session_dir'),
                               'epoch': pane.get('session_epoch'), 'observer': pane.get('observe'),
                               'recording': pane.get('recording'),
                               'transcript_id': pane.get('transcript_id')}
                              if session else None),
        })
    return {'schema': 'kilix.panes/v1', 'generated_at': time.time(),
            'self_pane_id': own, 'counts': {'pages': len(page_ids), 'panes': len(records)},
            'broker_available': False,
            'warnings': ['Codex rollout and Claude registry turn boundaries are available; broker telemetry is unavailable'],
            'panes': records}


def resolve(snapshot, target):
    target = target.strip()
    if not target:
        raise ValueError('A pane target is required')
    if target.startswith('pane:'):
        target = target[5:]
    panes = snapshot['panes']
    if target.isascii() and target.isdigit():
        match = next((p for p in panes if p['pane_id'] == int(target)), None)
        if match:
            return match
        raise ValueError(f'No live pane has ID {target}')
    folded = target.casefold()
    groups = (
        [p for p in panes if p['batty_session'] and
         p['batty_session']['name'].casefold().startswith(folded)],
        [p for p in panes if p['coding_session'] and
         p['coding_session']['session_id'] != 'unknown' and
         p['coding_session']['session_id'].casefold().startswith(folded)],
        [p for p in panes if p['title'].casefold() == folded],
        [p for p in panes if folded in p['title'].casefold() or
         folded in p['page']['title'].casefold() or
         (p['cwd'] and folded in p['cwd'].casefold())],
    )
    matches = next((group for group in groups if group), [])
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f'No live pane matches {target!r}')
    choices = ', '.join(f'{p["pane_id"]}:{p["title"]}' for p in matches)
    raise ValueError(f'Pane target {target!r} is ambiguous: {choices}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', help='Explicit control endpoint; otherwise discover this pane')
    parser.add_argument('--json', action='store_true', help='Print the joined pane snapshot')
    sub = parser.add_subparsers(dest='action')
    listing = sub.add_parser('list', help='List pages and panes')
    listing.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    dump = sub.add_parser('dump', help='Read visible plain text from one pane')
    dump.add_argument('target')
    dump.add_argument('--lines', type=int, help='Limit output to the last N lines')
    sub.add_parser('focus', help='Focus a pane').add_argument('target')
    send = sub.add_parser('send', help='Send bounded literal text to a pane')
    send.add_argument('target')
    send.add_argument('--enter', action='store_true', help='Append carriage return')
    send.add_argument('message', nargs='?')
    wait = sub.add_parser('wait', help='Wait for a live agent idle boundary')
    wait.add_argument('target')
    wait.add_argument('--for', dest='condition', choices=('idle',), required=True)
    wait.add_argument('--timeout', type=float, default=300)
    for command in sub.choices.values():
        command.add_argument('--socket', default=argparse.SUPPRESS)
    args, extra = parser.parse_known_args()
    # Python 3.12 leaves a trailing optional message unparsed when it follows
    # --enter in a subparser. Accept that documented order on both runtimes.
    if args.action == 'send' and args.message is None and len(extra) == 1 and not extra[0].startswith('-'):
        args.message = extra.pop()
    if extra:
        parser.error('unrecognized arguments: ' + ' '.join(extra))
    if args.json and args.action not in (None, 'list'):
        parser.error('--json is only valid for list')
    path = resolve_endpoint(args.socket)
    if args.action is None and not args.json:
        request(path, 'pane-center')
        return
    snapshot = pane_snapshot(path)
    if args.action in (None, 'list'):
        if args.json:
            print(json.dumps(snapshot, ensure_ascii=True))
        else:
            print('FOCUS  PANE  PAGE  ACTIVITY  PROCESS  TITLE  CWD')
            for pane in snapshot['panes']:
                print(f'{"*" if pane["focused"] else " "}  {pane["pane_id"]}  '
                      f'{pane["page"]["index"]}  {pane["activity"]}  '
                      f'{plain(pane["process"]["name"])}  '
                      f'{plain(pane["title"])}  {plain(pane["cwd"])}')
        return
    target = resolve(snapshot, args.target)
    pane_id = target['pane_id']
    if args.action == 'wait':
        if not 0 < args.timeout <= 86400:
            parser.error('--timeout must be greater than zero and at most 86400 seconds')
        coding = target['coding_session']
        supported = coding and ((coding['provider'] == 'codex' and coding['path']) or
                                (coding['provider'] == 'claude' and coding['session_id'] != 'unknown'))
        if not supported:
            raise RuntimeError('idle waiting requires a live Codex rollout owner or validated Claude registry')
        owner = (coding['provider'], coding['session_id'], coding['path'], tuple(coding['live_pids']))
        deadline = time.monotonic() + args.timeout
        while True:
            current = next((p for p in snapshot['panes'] if p['pane_id'] == pane_id), None)
            active = current['coding_session'] if current else None
            if not active or (active['provider'], active['session_id'], active['path'],
                              tuple(active['live_pids'])) != owner:
                raise RuntimeError('the agent session owner changed while waiting')
            if current['activity'] == 'idle':
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(f'timed out waiting for pane {pane_id} to become idle')
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
            snapshot = pane_snapshot(path)
    if args.action == 'focus':
        request(path, 'focus', pane_id)
        return
    if args.action == 'dump':
        if args.lines is not None and not 1 <= args.lines <= 1000:
            parser.error('--lines must be between 1 and 1000')
        value = request(path, 'dump', pane_id)
        if args.lines is not None:
            value = '\n'.join(value.splitlines()[-args.lines:]) + ('\n' if value else '')
        sys.stdout.write(value)
        return
    if args.message is None:
        parser.error('send requires literal text; use an empty string to send only Return')
    if '\0' in args.message:
        parser.error('send text cannot contain NUL bytes')
    payload = args.message.encode('utf-8') + (b'\r' if args.enter else b'')
    if len(payload) > 1024:
        parser.error('send text exceeds the 1024-byte limit')
    request(path, 'send', pane_id, payload)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, ValueError, RuntimeError) as error:
        print(f'kilix panes: {error}', file=sys.stderr)
        sys.exit(1)
