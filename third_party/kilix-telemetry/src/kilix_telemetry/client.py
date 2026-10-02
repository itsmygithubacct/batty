"""Stable client API with shared-ring reads and a direct-reader fallback."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

from .collect import LinuxCollector
from .model import PaneMetrics, Snapshot
from .registry import DEFAULT_STALE_SECONDS, PaneRegistry
from .ring import (
    RingReader,
    RingUnavailable,
    TelemetryError,
    TelemetryPaths,
    daemon_running,
    resolve_paths,
)

_TRUTHY = {"1", "true", "yes", "on"}


def _disabled() -> bool:
    return os.environ.get("KILIX_TELEMETRY_DISABLE", "").strip().lower() in _TRUTHY


def _daemon_command() -> list[str]:
    configured = os.environ.get("KILIX_TELEMETRY_COMMAND", "").strip()
    if configured:
        return shlex.split(configured)
    return [sys.executable, "-m", "kilix_telemetry", "serve", "--quiet"]


def _spawn_environment(paths: TelemetryPaths) -> dict[str, str]:
    environment = dict(os.environ)
    package_root = str(Path(__file__).resolve().parents[1])
    current = environment.get("PYTHONPATH", "")
    entries = [entry for entry in current.split(os.pathsep) if entry]
    if package_root not in entries:
        environment["PYTHONPATH"] = os.pathsep.join([package_root, *entries])
    environment["KILIX_TELEMETRY_RUNTIME"] = str(paths.directory)
    return environment


def _fresh_reader(paths: TelemetryPaths, cached: RingReader | None) -> RingReader | None:
    """Return a reader for the current ring file, reusing a valid mapping.

    Re-mapping the whole ring for every read costs a page-table cycle per
    poll; a cached mapping stays valid while the path still names the same
    inode with the same size. A replaced or resized ring (the writer renames
    a new file over the path on geometry change) is detected by comparing the
    path against the mapped file, and the stale reader is closed.
    """
    if cached is not None:
        try:
            named = os.stat(paths.ring)
            mapped = os.fstat(cached.fd)
            if (
                named.st_ino == mapped.st_ino
                and named.st_dev == mapped.st_dev
                and named.st_size == cached.size
            ):
                return cached
        except OSError:
            pass
        cached.close()
    try:
        return RingReader(paths)
    except (OSError, RingUnavailable):
        return None


def _writer_active(paths: TelemetryPaths) -> bool:
    try:
        return daemon_running(paths)
    except (OSError, TelemetryError):
        return False


def ensure_running(
    paths: TelemetryPaths | None = None,
    *,
    timeout: float = 2.5,
) -> bool:
    """Ensure a fresh writer exists without making consumers depend on it."""
    if _disabled():
        return False
    paths = paths or resolve_paths()
    if _writer_active(paths):
        return True
    command = _daemon_command()
    if not command:
        return False
    debug = os.environ.get("KILIX_TELEMETRY_DEBUG", "").lower() in _TRUTHY
    destination = None if debug else subprocess.DEVNULL
    started_ns = time.monotonic_ns()
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=destination,
            stderr=destination,
            close_fds=True,
            start_new_session=True,
            env=_spawn_environment(paths),
        )
    except OSError:
        return False
    deadline = time.monotonic() + max(0.0, timeout)
    reader: RingReader | None = None
    try:
        while time.monotonic() < deadline:
            reader = _fresh_reader(paths, reader)
            sample = None
            if reader is not None:
                try:
                    sample = reader.latest(max_age=3.0)
                except (OSError, RingUnavailable):
                    reader.close()
                    reader = None
            if (
                _writer_active(paths)
                and sample is not None
                and sample.monotonic_ns >= started_ns
            ):
                return True
            time.sleep(0.05)
        return _writer_active(paths)
    finally:
        if reader is not None:
            reader.close()


class TelemetryClient:
    def __init__(
        self,
        paths: TelemetryPaths | None = None,
        *,
        max_age: float = 5.0,
        cache_seconds: float = 0.2,
        fallback_root: str | Path = "/",
    ) -> None:
        self.paths = paths or resolve_paths()
        self.max_age = max(0.1, float(max_age))
        self.cache_seconds = max(0.0, float(cache_seconds))
        self._fallback = LinuxCollector(fallback_root)
        self._cached: Snapshot | None = None
        self._cached_until = 0.0
        self._lock = threading.Lock()
        self._reader: RingReader | None = None
        self._writer_alive = False
        self._writer_checked = float("-inf")
        # Registration bookkeeping has its own lock so registering a pane
        # never waits behind a snapshot poll that may be starting the daemon.
        self._registry_lock = threading.Lock()
        self._pane_roots: dict[int, float] = {}
        self._registered_roots: tuple[int, ...] = ()
        self._registered_at = 0.0

    def snapshot(
        self,
        *,
        start: bool = True,
        fallback: bool = True,
        force: bool = False,
    ) -> Snapshot | None:
        now = time.monotonic()
        if not force and self._cached is not None and now < self._cached_until:
            return self._cached
        with self._lock:
            now = time.monotonic()
            if not force and self._cached is not None and now < self._cached_until:
                return self._cached
            snapshot = self._read_ring()
            if start and not self._writer_probe(now):
                if ensure_running(self.paths):
                    self._writer_alive = True
                    self._writer_checked = time.monotonic()
                    refreshed = self._read_ring()
                    if refreshed is not None:
                        snapshot = refreshed
            if snapshot is None and fallback:
                snapshot = self._fallback.sample()
            self._cached = snapshot
            self._cached_until = now + self.cache_seconds
            return snapshot

    def _read_ring(self) -> Snapshot | None:
        """Read the newest ring record; the caller holds the client lock."""
        self._reader = _fresh_reader(self.paths, self._reader)
        if self._reader is None:
            return None
        try:
            return self._reader.latest(max_age=self.max_age)
        except (OSError, RingUnavailable):
            self._reader.close()
            self._reader = None
            return None

    def _writer_probe(self, now: float) -> bool:
        """Probe writer liveness, remembering the answer for one second.

        The lock-file probe stays authoritative - a fresh ring record from an
        exited writer never counts as liveness - but reusing the result
        briefly avoids an open and flock per poll and only bounds how quickly
        a daemon exit is noticed, by at most that second.
        """
        if now - self._writer_checked >= 1.0:
            self._writer_alive = _writer_active(self.paths)
            self._writer_checked = now
        return self._writer_alive

    def close(self) -> None:
        """Release the cached ring mapping."""
        with self._lock:
            if self._reader is not None:
                self._reader.close()
                self._reader = None

    def pane(
        self,
        root_pid: int,
        *,
        start: bool = True,
        fallback: bool = True,
        force: bool = False,
    ) -> PaneMetrics:
        self._register_pane(root_pid)
        snapshot = self.snapshot(start=start, fallback=False, force=force)
        if snapshot is None and fallback:
            snapshot = self._fallback.sample(pss_roots=(int(root_pid),))
        if snapshot is None:
            return PaneMetrics(max(0, int(root_pid)), 0, 0.0, 0, 0, False)
        return snapshot.pane(root_pid)

    def _register_pane(self, root_pid: int) -> None:
        """Register the union of every pane this process polls.

        One client polling several panes must keep every root visible to the
        sampler; registering only the most recent poll would flap the shared
        registry between singletons, resetting pane CPU deltas and PSS scope.
        Roots which have not been polled within the registry staleness window
        age out, and an unchanged set is re-registered only often enough to
        keep its record fresh. The bookkeeping and the registry write share
        one critical section so two threads polling different panes cannot
        interleave into recording a union that was never written.
        """
        if _disabled():
            return
        try:
            pid = int(root_pid)
        except (TypeError, ValueError):
            return
        if pid <= 0:
            return
        with self._registry_lock:
            now = time.monotonic()
            self._pane_roots[pid] = now
            horizon = now - DEFAULT_STALE_SECONDS
            for known, seen in list(self._pane_roots.items()):
                if seen < horizon:
                    del self._pane_roots[known]
            union = tuple(sorted(self._pane_roots))
            if (
                union == self._registered_roots
                and now - self._registered_at < DEFAULT_STALE_SECONDS / 3.0
            ):
                return
            if self._write_pane_roots(union):
                self._registered_roots = union
                self._registered_at = now

    def _write_pane_roots(self, roots: tuple[int, ...]) -> bool:
        try:
            PaneRegistry(self.paths).update(os.getpid(), roots)
        except (OSError, ValueError):
            return False
        return True

    def register_panes(self, roots: tuple[int, ...] | list[int]) -> bool:
        if _disabled():
            return False
        with self._registry_lock:
            if not self._write_pane_roots(tuple(roots)):
                return False
            now = time.monotonic()
            accepted: dict[int, float] = {}
            for item in tuple(roots):
                try:
                    pid = int(item)
                except (TypeError, ValueError):
                    continue
                if pid > 0:
                    accepted[pid] = now
            self._pane_roots = accepted
            self._registered_roots = tuple(sorted(accepted))
            self._registered_at = now
            return True

    def history(
        self,
        limit: int | None = None,
        *,
        max_age: float | None = None,
    ) -> tuple[Snapshot, ...]:
        try:
            with RingReader(self.paths) as reader:
                return reader.history(limit, max_age=max_age)
        except (OSError, RingUnavailable):
            return ()


_DEFAULT_CLIENT: TelemetryClient | None = None
_DEFAULT_LOCK = threading.Lock()


def default_client() -> TelemetryClient:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        with _DEFAULT_LOCK:
            if _DEFAULT_CLIENT is None:
                _DEFAULT_CLIENT = TelemetryClient()
    return _DEFAULT_CLIENT
