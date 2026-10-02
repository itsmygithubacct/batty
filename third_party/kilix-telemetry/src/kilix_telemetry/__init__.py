"""Shared, low-overhead Linux telemetry for Kilix components."""

from .client import TelemetryClient, ensure_running
from .collect import LinuxCollector
from .model import (
    FanSensor,
    PaneMetrics,
    ProcessMetrics,
    Snapshot,
    SystemMetrics,
    ThermalSensor,
)
from .registry import PaneRegistry
from .ring import RingReader, RingWriter, TelemetryPaths, daemon_running, resolve_paths

__version__ = "0.1.2"
TELEMETRY_API_VERSION = (1, 2)

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
    "__version__",
    "daemon_running",
    "ensure_running",
    "resolve_paths",
]
