#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Version 1 client for Batty's private workspace control endpoint."""
import argparse
import json
import os
import socket
import struct
import sys

LIMIT = 131072
OPERATIONS = {name: i for i, name in enumerate(
    ('ping', 'list', 'focus', 'send', 'paste', 'dump', 'close', 'zoom', 'info',
     'launch', 'resize', 'sync', 'move', 'session', 'events', 'rename', 'checkpoint',
     'pane-center', 'telemetry', 'pane-center-state', 'layout-apply', 'pane-rename',
     'reload-settings', 'reload-status', 'message', 'font-size'), 1)}
DIRECTIONS = ('left', 'right', 'up', 'down')


def request(path, operation, pane=0, payload=b'', timeout=5):
    if not path:
        raise ValueError('Set BATTY_CONTROL or supply --socket PATH')
    if not 0 <= pane < 2**64:
        raise ValueError('Pane ID must be an unsigned 64-bit integer')
    packet = struct.pack('<4sB3xQ', b'BTC1', OPERATIONS[operation], pane) + payload
    if len(packet) > LIMIT:
        raise ValueError('Request exceeds control packet limit')
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
        connection.settimeout(timeout)
        connection.connect(path)
        connection.sendall(packet)
        response, _, flags, _ = connection.recvmsg(LIMIT)
    if flags & socket.MSG_TRUNC or len(response) < 8 or response[:4] != b'BTC1':
        raise RuntimeError('Missing or invalid version 1 control response')
    status, = struct.unpack_from('<I', response, 4)
    text = response[8:].decode('utf-8', errors='replace')
    if status:
        raise RuntimeError(text)
    return text if operation == 'dump' else json.loads(text)


def events(path, after=0, epoch='0', timeout=5):
    """Wait up to one second for changes; reset responses include current panes."""
    generation = int(epoch, 16)
    if not 0 <= generation < 2**64:
        raise ValueError('Event epoch must be an unsigned 64-bit hexadecimal integer')
    return request(path, 'events', after, struct.pack('<Q', generation), timeout)


def apply_layout(path, layout_hex, mapping):
    try:
        layout = bytes.fromhex(layout_hex)
    except (TypeError, ValueError) as error:
        raise ValueError('Invalid workspace layout encoding') from error
    if (not 0 < len(layout) <= 32768 or layout[:4] not in (b'BWL1', b'BWL2') or
            not 1 <= len(mapping) <= 64):
        raise ValueError('Invalid workspace layout or pane mapping')
    pairs = bytearray()
    for source, target in mapping:
        if (type(source) is not int or type(target) is not int or
                not 0 < source < 2**64 or not 0 < target < 2**64):
            raise ValueError('Pane mapping IDs must be unsigned 64-bit values')
        pairs.extend(struct.pack('<QQ', source, target))
    payload = struct.pack('<IH', len(layout), len(mapping)) + layout + pairs
    return request(path, 'layout-apply', payload=payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', help='Explicit endpoint; otherwise discover the calling pane')
    sub = parser.add_subparsers(dest='operation', required=True)
    for name in OPERATIONS:
        if name in ('session', 'telemetry', 'layout-apply'):
            continue  # Session launch and internal telemetry use typed clients.
        p = sub.add_parser(name)
        if name not in ('ping', 'list', 'launch', 'events', 'checkpoint', 'pane-center', 'pane-center-state', 'reload-settings', 'message', 'font-size'):
            p.add_argument('pane', type=int)
        if name in ('send', 'paste'):
            p.add_argument('text', help='Use - to read bytes from standard input')
        elif name in ('rename', 'pane-rename'):
            p.add_argument('title', help='Custom title; an empty string restores the terminal-derived title')
        elif name == 'message':
            p.add_argument('text', help='Printable on-screen status, or empty to clear')
        elif name == 'font-size':
            p.add_argument('size', type=int, help='Set every pane and the default in this workspace (6–96 pixels)')
        elif name == 'events':
            p.add_argument('--after', type=int, default=0, help='Resume after this event cursor')
            p.add_argument('--epoch', default='0', help='Endpoint epoch from a previous response')
            p.add_argument('--follow', action='store_true', help='Continue waiting for changes')
        elif name == 'launch':
            p.add_argument('--target', type=int, default=0)
            p.add_argument('--direction', choices=DIRECTIONS, default='right')
            p.add_argument('--session', help='Create or reconnect a named session')
            mode = p.add_mutually_exclusive_group()
            mode.add_argument('--attach', action='store_true', help='Require an existing named session')
            mode.add_argument('--observe', action='store_true', help='Observe an existing named session')
            p.add_argument('command', nargs=argparse.REMAINDER)
        elif name == 'resize':
            p.add_argument('axis', choices=('horizontal', 'vertical'))
            p.add_argument('delta', type=int, help='Change pane share in basis points')
        elif name == 'sync':
            p.add_argument('enabled', choices=('on', 'off'))
        elif name == 'move':
            p.add_argument('direction', choices=DIRECTIONS)
    args = parser.parse_args()
    payload = b''
    operation = args.operation
    pane = getattr(args, 'pane', 0)
    if args.operation in ('send', 'paste'):
        payload = sys.stdin.buffer.read(LIMIT) if args.text == '-' else args.text.encode()
    elif args.operation in ('rename', 'pane-rename'):
        payload = args.title.encode()
    elif args.operation == 'message':
        payload = args.text.encode()
    elif args.operation == 'launch':
        command = args.command[1:] if args.command[:1] == ['--'] else args.command
        attaching = args.attach or args.observe
        if (attaching and command) or (not attaching and not command) or any('\0' in arg for arg in command):
            parser.error('launch requires a command; attach/observe take no command; NUL bytes are not allowed')
        if attaching and not args.session:
            parser.error('attach/observe require --session NAME')
        pane = args.target
        payload = bytes((DIRECTIONS.index(args.direction),))
        if args.session is not None:
            if not args.session or '\0' in args.session:
                parser.error('session name must be nonempty and contain no NUL bytes')
            operation = 'session'
            payload += bytes((2 if args.observe else 1 if args.attach else 0,)) + args.session.encode() + b'\0'
        if command:
            payload += b'\0'.join(os.fsencode(arg) for arg in command) + b'\0'
    elif args.operation == 'resize':
        if not -9998 <= args.delta <= 9998:
            parser.error('resize delta must be between -9998 and 9998')
        payload = struct.pack('<Bi', args.axis == 'horizontal', args.delta)
    elif args.operation == 'sync':
        payload = bytes((args.enabled == 'on',))
    elif args.operation == 'move':
        payload = bytes((DIRECTIONS.index(args.direction),))
    elif args.operation == 'font-size':
        if not 6 <= args.size <= 96:
            parser.error('font size must be between 6 and 96 pixels')
        payload = bytes((args.size,))
    from control_paths import resolve_endpoint
    path = resolve_endpoint(args.socket)
    if operation == 'events':
        after, epoch = args.after, args.epoch
        while True:
            result = events(path, after, epoch)
            if result['reset'] or result['events'] or not args.follow:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            if not args.follow:
                return
            after, epoch = result['cursor'], result['epoch']
    result = request(path, operation, pane, payload)
    if isinstance(result, str):
        sys.stdout.write(result)
    else:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print(f'battyctl: {error}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
