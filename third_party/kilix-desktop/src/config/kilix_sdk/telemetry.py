"""Pinned shared telemetry client exposed through the Kilix host SDK."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ._packages import load_pinned_package
from . import paths as host_paths


def _load_shared_package():
    root = Path(__file__).resolve().parents[2]
    package = load_pinned_package(
        "kilix_telemetry",
        (
            root / "third_party" / "kilix-telemetry" / "src",
            root.parent / "kilix-modules" / "kilix-telemetry" / "src",
        ),
        "kilix-telemetry is unavailable; initialize Kilix submodules with: "
        "git submodule update --init --recursive",
    )
    try:
        major, minor = package.TELEMETRY_API_VERSION
    except (AttributeError, TypeError, ValueError) as error:
        raise ImportError("kilix-telemetry has no compatible API version") from error
    if int(major) != 1 or int(minor) < 2:
        raise ImportError(
            "kilix-telemetry API 1.2 or newer is required; "
            f"found {major}.{minor}"
        )
    return package


_shared = _load_shared_package()

TELEMETRY_API_VERSION = _shared.TELEMETRY_API_VERSION
FanSensor = _shared.FanSensor
LinuxCollector = _shared.LinuxCollector
PaneMetrics = _shared.PaneMetrics
PaneRegistry = _shared.PaneRegistry
ProcessMetrics = _shared.ProcessMetrics
RingReader = _shared.RingReader
RingWriter = _shared.RingWriter
Snapshot = _shared.Snapshot
SystemMetrics = _shared.SystemMetrics
TelemetryPaths = _shared.TelemetryPaths
ThermalSensor = _shared.ThermalSensor
telemetry_version = _shared.__version__


def resolve_paths(runtime: str | Path | None = None) -> TelemetryPaths:
    """Resolve the host's private ring unless a caller selects another one."""
    return _shared.resolve_paths(runtime or host_paths.telemetry_dir())


class TelemetryClient(_shared.TelemetryClient):
    """A telemetry client defaulting to the Kilix host's private ring."""

    def __init__(self, paths: TelemetryPaths | None = None, **kwargs) -> None:
        super().__init__(paths or resolve_paths(), **kwargs)


def ensure_running(
    paths: TelemetryPaths | None = None,
    *,
    timeout: float = 2.5,
) -> bool:
    return _shared.ensure_running(paths or resolve_paths(), timeout=timeout)


def daemon_running(paths: TelemetryPaths | None = None) -> bool:
    """Return whether the pinned singleton sampler currently owns its lock."""
    return _shared.daemon_running(paths or resolve_paths())


@lru_cache(maxsize=1)
def default_client() -> TelemetryClient:
    """Return this process's client for the shared per-user ring."""
    return TelemetryClient()


__all__ = [
    "TELEMETRY_API_VERSION",
    "FanSensor",
    "LinuxCollector",
    "PaneMetrics",
    "PaneRegistry",
    "ProcessMetrics",
    "RingReader",
    "RingWriter",
    "Snapshot",
    "SystemMetrics",
    "TelemetryClient",
    "TelemetryPaths",
    "ThermalSensor",
    "daemon_running",
    "default_client",
    "ensure_running",
    "resolve_paths",
    "telemetry_version",
]
