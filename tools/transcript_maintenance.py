#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Frontend-owned, bounded background transcript maintenance."""
import argparse
import os
import select
import sys

import kilix_transcript as api
from kilix_settings import read_settings, settings_path, transcript_value
import transcript_storage as storage


def watch(parent, interval=60, operations=16, seconds=2):
    # Verify the actual parent relationship around pidfd acquisition: a dead
    # frontend cannot accidentally be replaced by a reused numeric PID.
    if parent <= 1 or os.getppid() != parent:
        return
    owner = os.pidfd_open(parent)
    try:
        if os.getppid() != parent:
            return
        poller = select.poll()
        poller.register(owner, select.POLLIN)
        storage.cancelled = lambda: bool(poller.poll(0))
        diagnostic = None
        while not storage.cancelled():
            root = -1
            try:
                root = api.open_root(api.directory())
                # Other frontends wait without rendering-thread work. If this
                # frontend exits, one surviving frontend takes over the lock.
                with storage.maintenance_lock(root, True, '.watch.lock'):
                    while not storage.cancelled():
                        delay = interval
                        try:
                            values = read_settings(settings_path())
                            with storage.maintenance_lock(root, True):
                                result = storage.maintain(root, api,
                                    api.budget(transcript_value(values, 'transcript_total')),
                                    api.budget(transcript_value(values, 'transcript_archive_total')),
                                    max_operations=operations, max_seconds=seconds)
                            if result['deferred']:
                                delay = 1
                            elif result['over_budget']:
                                raise ValueError('Protected transcripts prevent meeting retention budgets')
                            diagnostic = None
                        except storage.MaintenanceBusy:
                            delay = 1
                        except InterruptedError:
                            return
                        except (OSError, ValueError) as error:
                            message = str(error)
                            if message != diagnostic:
                                print('Kilix transcript maintenance: ' + message, file=sys.stderr, flush=True)
                                diagnostic = message
                        if poller.poll(int(delay * 1000)):
                            return
            except storage.MaintenanceBusy:
                if poller.poll(1000):
                    return
            except FileNotFoundError:
                # The first recorder may still be creating the default path,
                # or recording may be off with no existing history to maintain.
                if poller.poll(1000):
                    return
            except (OSError, ValueError) as error:
                message = str(error)
                if message != diagnostic:
                    print('Kilix transcript maintenance: ' + message, file=sys.stderr, flush=True)
                    diagnostic = message
                if poller.poll(int(interval * 1000)):
                    return
            finally:
                if root >= 0:
                    os.close(root)
    finally:
        storage.cancelled = lambda: False
        os.close(owner)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('parent', type=int)
    parser.add_argument('--interval', type=float, default=60)
    parser.add_argument('--operations', type=int, default=16)
    parser.add_argument('--seconds', type=float, default=2)
    args = parser.parse_args()
    if not 0.05 <= args.interval <= 3600 or not 1 <= args.operations <= 128 or not 0.01 <= args.seconds <= 60:
        parser.error('Invalid maintenance pass limits')
    watch(args.parent, args.interval, args.operations, args.seconds)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print(f'Kilix transcript maintenance: {error}', file=sys.stderr)
        raise SystemExit(1)
