#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Bounded, live-owner coding-agent telemetry for Kilix panes."""
import json
from itertools import islice
import os
from pathlib import Path
import re
import stat

UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)
STARTED = frozenset(('task_started', 'turn_started'))
COMPLETE = frozenset(('task_complete', 'turn_complete', 'turn_completed'))
TAIL_LIMIT = 8 * 1024 * 1024
LINE_LIMIT = 1024 * 1024
FD_LIMIT = 256
REGISTRY_LIMIT = 65536


def agent_name(record):
    names = {Path(value).name.casefold() for value in record['argv'][:2]}
    if 'codex' in names or record['name'] == 'codex':
        return 'codex'
    if 'claude' in names or record['name'] == 'claude':
        return 'claude'
    if 'kimi' in names or 'kimi-code' in names:
        return 'kimi'
    if 'aider' in names or record['name'] == 'aider':
        return 'aider'
    return ''


def owned_rollouts(pid):
    """Find only rollout files actually open by this same-user process."""
    root = Path('/proc') / str(pid)
    try:
        if root.stat().st_uid != os.getuid():
            return []
        with os.scandir(root / 'fd') as entries:
            names = [entry.name for entry in islice(entries, FD_LIMIT)]
    except OSError:
        return []
    paths = []
    for name in names:
        if not name.isdigit():
            continue
        try:
            descriptor = root / 'fd' / name
            path = os.readlink(descriptor)
            if path.endswith(' (deleted)'):
                continue
            candidate = Path(path)
            if not candidate.is_absolute() or not candidate.name.startswith('rollout-') or candidate.suffix != '.jsonl':
                continue
            status = candidate.stat()
            open_status = descriptor.stat()
            if (status.st_uid == os.getuid() and stat.S_ISREG(status.st_mode)
                    and (status.st_dev, status.st_ino) == (open_status.st_dev, open_status.st_ino)):
                paths.append((candidate, status.st_mtime, (status.st_dev, status.st_ino)))
        except OSError:
            continue
    return paths


def operator_text(value):
    if isinstance(value, str):
        pieces = [value]
    elif isinstance(value, list):
        pieces = []
        for part in value[:32]:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict):
                pieces.append(part.get('text') or part.get('input_text'))
    else:
        return ''
    text = ' '.join(' '.join(part.split()) for part in pieces if isinstance(part, str))[:500]
    if text.casefold().startswith(('<codex_internal_context', '<environment_context',
                                   '<permissions instructions>', '<skills_instructions>')):
        return ''
    return text


def rollout_details(path, identity):
    """Read a bounded tail for the latest complete turn and operator task."""
    try:
        with path.open('rb') as stream:
            status = os.fstat(stream.fileno())
            if (status.st_dev, status.st_ino) != identity:
                return '', ''
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            count = min(end, TAIL_LIMIT)
            stream.seek(end - count)
            data = stream.read(count)
    except OSError:
        return '', ''
    # The first line may be clipped by the bound; the last may still be writing.
    lines = data.split(b'\n')
    if count < end:
        lines.pop(0)
    if lines and lines[-1]:
        return '', ''  # A partially written next turn could invalidate the last boundary.
    event, prompt = '', ''
    for raw in reversed(lines):
        if len(raw) > LINE_LIMIT or (b'event_msg' not in raw and b'response_item' not in raw):
            continue
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get('payload')
        if not isinstance(payload, dict):
            continue
        if record.get('type') == 'event_msg':
            kind = payload.get('type')
            if event and not prompt and (kind in COMPLETE or
                                         kind in STARTED and event in STARTED):
                break  # Do not borrow a task from an older turn.
            if not event and kind in STARTED | COMPLETE:
                event = kind
            if not prompt and kind == 'user_message':
                prompt = operator_text(payload.get('message') or payload.get('text'))
        elif (record.get('type') == 'response_item' and payload.get('type') == 'message'
              and payload.get('role') == 'user' and not prompt):
            prompt = operator_text(payload.get('content'))
        if event and prompt:
            break
    return event, prompt


def claude_registry(pid):
    """Read this PID's live Claude descriptor without scanning other sessions."""
    root = Path('/proc') / str(pid)
    try:
        if root.stat().st_uid != os.getuid():
            return None
        raw = (root / 'stat').read_text()
        end = raw.rfind(')')
        if end < 0:
            return None
        fields = raw[end + 2:].split()
        if len(fields) <= 19:
            return None
        start_ticks = fields[19]
        config = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
        path = config / 'sessions' / f'{pid}.json'
        status = path.stat()
        if status.st_uid != os.getuid() or not stat.S_ISREG(status.st_mode) or status.st_size > REGISTRY_LIMIT:
            return None
        with path.open('rb') as stream:
            data = stream.read(REGISTRY_LIMIT + 1)
        if len(data) > REGISTRY_LIMIT:
            return None
        record = json.loads(data)
        if (not isinstance(record, dict) or record.get('pid') != pid or
                str(record.get('procStart') or '') != start_ticks):
            return None
        session_id = record.get('sessionId')
        if not isinstance(session_id, str) or not 0 < len(session_id) <= 128:
            return None
        return record, status.st_mtime
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def coding_session(records, cwd):
    """Return a live agent session and activity, never an inferred idle state."""
    for record in reversed(records):
        provider = agent_name(record)
        if not provider:
            continue
        pid = record['pid']
        paths = owned_rollouts(pid) if provider == 'codex' else []
        registry = claude_registry(pid) if provider == 'claude' else None
        if paths:
            path, updated, identity = max(paths, key=lambda item: item[1])
            event, prompt = rollout_details(path, identity)
            match = UUID.search(path.name)
            session_id = match.group(0).lower() if match else 'unknown'
            status = 'idle' if event in COMPLETE else 'working' if event in STARTED else 'unknown'
            title = prompt; version = ''; entrypoint = ''
        elif registry:
            record, updated = registry
            path, event = None, ''
            session_id = record['sessionId'].lower()
            reported = str(record.get('status') or '').casefold().replace('_', '-')
            status = ('idle' if reported in ('idle', 'ready') else
                      'working' if reported in ('working', 'active', 'busy', 'running') else
                      'waiting' if reported in ('waiting', 'wait', 'blocked') else 'unknown')
            cwd = str(record.get('cwd') or cwd)
            title = str(record.get('name') or '')[:160]
            prompt = ''
            version = str(record.get('version') or '')[:64]
            entrypoint = str(record.get('entrypoint') or '')[:128]
        else:
            path, updated, event, session_id, status = None, 0, '', 'unknown', 'unknown'
            title = ''; version = ''; entrypoint = ''
            prompt = ''
        session = {
            'provider': provider, 'session_id': session_id,
            'state': 'live', 'legacy_state': 'live', 'resumable': False,
            'project': Path(cwd).name if cwd else '(unknown)', 'cwd': cwd,
            'original_cwd': cwd, 'cwd_exists': bool(cwd and os.path.isdir(cwd)),
            'title': title, 'updated': updated, 'started': None,
            'path': str(path) if path else None, 'live_pids': [pid],
            'live_status': status, 'last_user_message': prompt,
            'last_agent_message': '', 'last_turn_event': event,
            'pending_tool': '', 'git_branch': '', 'version': version,
            'entrypoint': entrypoint, 'archived': False, 'invalid_reason': '',
        }
        return session, status if status in ('idle', 'working', 'waiting') else 'agent'
    return None, None
