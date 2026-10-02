"""Lifecycle for the single per-user Kilix telemetry sampler."""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

from .collect import LinuxCollector
from .registry import PaneRegistry
from .ring import (
    DEFAULT_SLOT_COUNT,
    DEFAULT_SLOT_SIZE,
    DaemonLock,
    RingBusy,
    RingWriter,
    TelemetryError,
    TelemetryPaths,
    resolve_paths,
)


def run_daemon(
    *,
    paths: TelemetryPaths | None = None,
    root: str | Path = "/",
    interval: float = 1.0,
    pss_interval: float = 2.0,
    slot_count: int = DEFAULT_SLOT_COUNT,
    slot_size: int = DEFAULT_SLOT_SIZE,
    once: bool = False,
    ready: Callable[[], None] | None = None,
) -> int:
    """Run the sampler in the foreground.

    The caller owns daemonization. Keeping this function foreground-only makes
    it work under a terminal session, systemd --user, tests, and a simple
    start_new_session subprocess without maintaining four lifecycle paths.
    """
    paths = paths or resolve_paths()
    interval = max(0.1, min(60.0, float(interval)))
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    previous_handlers: dict[int, object] = {}
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous_handlers[signum] = signal.signal(signum, stop)
    try:
        with (
            DaemonLock(paths),
            RingWriter(paths, slot_count=slot_count, slot_size=slot_size) as ring,
        ):
            collector = LinuxCollector(root, pss_interval=pss_interval)
            registry = PaneRegistry(paths)
            deadline = time.monotonic()
            first = True
            while not stopping:
                snapshot = collector.sample(pss_roots=registry.roots())
                try:
                    ring.publish(snapshot)
                except TelemetryError:
                    if once:
                        return 1
                else:
                    if first:
                        first = False
                        if ready is not None:
                            ready()
                if once:
                    break
                deadline += interval
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    deadline = time.monotonic()
                    continue
                end = time.monotonic() + remaining
                while not stopping:
                    pause = end - time.monotonic()
                    if pause <= 0:
                        break
                    time.sleep(min(0.25, pause))
        return 0
    except RingBusy:
        return 0
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def configured_interval() -> float:
    try:
        return float(os.environ.get("KILIX_TELEMETRY_INTERVAL", "1.0"))
    except ValueError:
        return 1.0


def configured_pss_interval() -> float:
    try:
        return float(os.environ.get("KILIX_TELEMETRY_PSS_INTERVAL", "2.0"))
    except ValueError:
        return 2.0
