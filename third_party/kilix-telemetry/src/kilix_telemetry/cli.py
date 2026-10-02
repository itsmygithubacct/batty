"""Command-line and daemon entry points for kilix-telemetry."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .client import TelemetryClient
from .collect import LinuxCollector
from .daemon import configured_interval, configured_pss_interval, run_daemon
from .ring import (
    DEFAULT_SLOT_COUNT,
    DEFAULT_SLOT_SIZE,
    RingReader,
    RingUnavailable,
    TelemetryError,
    daemon_running,
    resolve_paths,
)


def _snapshot_dict(snapshot, *, processes: bool = True) -> dict[str, object]:
    result = snapshot.to_dict()
    if not processes:
        result["processes"] = []
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kilix-telemetry",
        description="Shared CPU, memory, pressure, thermal, and process telemetry.",
    )
    parser.add_argument("--runtime", help="private telemetry runtime directory")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="run the single sampler")
    serve.add_argument(
        "--runtime", dest="command_runtime", help="private telemetry runtime directory"
    )
    serve.add_argument("--root", default="/", help="injectable proc/sys root")
    serve.add_argument("--interval", type=float, default=configured_interval())
    serve.add_argument("--pss-interval", type=float, default=configured_pss_interval())
    serve.add_argument("--slots", type=int, default=DEFAULT_SLOT_COUNT)
    serve.add_argument("--slot-size", type=int, default=DEFAULT_SLOT_SIZE)
    serve.add_argument("--once", action="store_true")
    serve.add_argument("--quiet", action="store_true")

    snapshot = subparsers.add_parser("snapshot", help="print the newest sample")
    snapshot.add_argument(
        "--runtime", dest="command_runtime", help="private telemetry runtime directory"
    )
    snapshot.add_argument("--direct", action="store_true")
    snapshot.add_argument("--no-start", action="store_true")
    snapshot.add_argument("--no-processes", action="store_true")
    snapshot.add_argument("--root", default="/")

    pane = subparsers.add_parser("pane", help="print one process-tree sample")
    pane.add_argument(
        "--runtime", dest="command_runtime", help="private telemetry runtime directory"
    )
    pane.add_argument("pid", type=int)
    pane.add_argument("--no-start", action="store_true")

    history = subparsers.add_parser("history", help="print recent ring samples")
    history.add_argument(
        "--runtime", dest="command_runtime", help="private telemetry runtime directory"
    )
    history.add_argument("--limit", type=int, default=10)
    history.add_argument("--no-processes", action="store_true")

    status = subparsers.add_parser("status", help="report daemon and ring health")
    status.add_argument(
        "--runtime", dest="command_runtime", help="private telemetry runtime directory"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    runtime = getattr(arguments, "command_runtime", None) or arguments.runtime
    paths = resolve_paths(runtime)
    command = arguments.command or "status"
    if command == "serve":
        result = run_daemon(
            paths=paths,
            root=Path(arguments.root),
            interval=arguments.interval,
            pss_interval=arguments.pss_interval,
            slot_count=arguments.slots,
            slot_size=arguments.slot_size,
            once=arguments.once,
        )
        if not arguments.quiet and result == 0:
            print(f"kilix-telemetry: ring {paths.ring}")
        return result
    if command == "snapshot":
        if arguments.direct:
            sample = LinuxCollector(arguments.root).sample()
        else:
            sample = TelemetryClient(paths, fallback_root=arguments.root).snapshot(
                start=not arguments.no_start, fallback=True, force=True
            )
        if sample is None:
            print("kilix-telemetry: no sample", file=sys.stderr)
            return 1
        print(
            json.dumps(
                _snapshot_dict(sample, processes=not arguments.no_processes),
                sort_keys=True,
            )
        )
        return 0
    if command == "pane":
        sample = TelemetryClient(paths).snapshot(
            start=not arguments.no_start, fallback=True, force=True
        )
        if sample is None:
            print("kilix-telemetry: no sample", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "sequence": sample.sequence,
                    **asdict(sample.pane(arguments.pid)),
                },
                sort_keys=True,
            )
        )
        return 0
    if command == "history":
        try:
            with RingReader(paths) as reader:
                samples = reader.history(arguments.limit)
        except (OSError, RingUnavailable) as error:
            print(f"kilix-telemetry: {error}", file=sys.stderr)
            return 1
        print(
            json.dumps(
                [
                    _snapshot_dict(sample, processes=not arguments.no_processes)
                    for sample in samples
                ],
                sort_keys=True,
            )
        )
        return 0
    if command == "status":
        try:
            running = daemon_running(paths)
        except (OSError, TelemetryError):
            running = False
        try:
            with RingReader(paths) as reader:
                sample = reader.latest(max_age=5.0)
        except (OSError, TelemetryError):
            sample = None
        if not running:
            print(f"stopped  ring={paths.ring}")
            return 1
        if sample is None:
            print(f"running  sequence=0 processes=0 ring={paths.ring}")
            return 0
        print(
            f"running  sequence={sample.sequence} "
            f"processes={len(sample.processes)} ring={paths.ring}"
        )
        return 0
    raise AssertionError(command)


def daemon_main() -> int:
    return main(["serve", "--quiet", *sys.argv[1:]])
