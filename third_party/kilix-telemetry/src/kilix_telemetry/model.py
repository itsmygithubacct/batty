"""Versioned telemetry records shared by the daemon and every consumer."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1


def _process_children(processes: Iterable[Any]) -> tuple[set[int], dict[int, list[int]]]:
    """Index one process table for descendant walks.

    Returns the set of live PIDs and a parent-to-children map. The elements
    only need ``pid`` and ``ppid`` attributes, so the sampler's raw records
    and shared ``ProcessMetrics`` use the same walk.
    """
    live: set[int] = set()
    children: dict[int, list[int]] = {}
    for process in processes:
        live.add(process.pid)
        children.setdefault(process.ppid, []).append(process.pid)
    return live, children


def _descendant_pids(
    live: set[int],
    children: Mapping[int, list[int]],
    roots: Iterable[int],
) -> set[int]:
    """Collect every listed root present in the table plus its descendants."""
    selected: set[int] = set()
    pending = list(roots)
    while pending:
        pid = pending.pop()
        if pid in selected or pid not in live:
            continue
        selected.add(pid)
        pending.extend(children.get(pid, ()))
    return selected


def _finite_float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return parsed


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True, slots=True)
class ThermalSensor:
    key: str
    chip: str
    label: str
    source: str
    celsius: float
    warning_celsius: float | None = None
    critical_celsius: float | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ThermalSensor:
        def optional(name: str) -> float | None:
            raw = value.get(name)
            return None if raw is None else _finite_float(raw)

        return cls(
            key=str(value.get("key", ""))[:256],
            chip=str(value.get("chip", ""))[:80],
            label=str(value.get("label", ""))[:80],
            source=str(value.get("source", ""))[:80],
            celsius=_finite_float(value.get("celsius")),
            warning_celsius=optional("warning_celsius"),
            critical_celsius=optional("critical_celsius"),
        )


@dataclass(frozen=True, slots=True)
class FanSensor:
    key: str
    chip: str
    label: str
    source: str
    rpm: int

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FanSensor:
        return cls(
            key=str(value.get("key", ""))[:256],
            chip=str(value.get("chip", ""))[:80],
            label=str(value.get("label", ""))[:80],
            source=str(value.get("source", ""))[:80],
            rpm=min(200_000, _nonnegative_int(value.get("rpm"))),
        )


@dataclass(frozen=True, slots=True)
class ProcessMetrics:
    pid: int
    ppid: int
    start_ticks: int
    cpu_ticks: int
    cpu_cores: float
    rss_bytes: int
    pss_bytes: int | None
    virtual_bytes: int
    uid: int
    name: str
    state: str
    threads: int
    command: str
    anon_bytes: int = 0
    file_bytes: int = 0
    shared_bytes: int = 0

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProcessMetrics:
        pss = value.get("pss_bytes")
        return cls(
            pid=_nonnegative_int(value.get("pid")),
            ppid=_nonnegative_int(value.get("ppid")),
            start_ticks=_nonnegative_int(value.get("start_ticks")),
            cpu_ticks=_nonnegative_int(value.get("cpu_ticks")),
            cpu_cores=max(0.0, _finite_float(value.get("cpu_cores"))),
            rss_bytes=_nonnegative_int(value.get("rss_bytes")),
            pss_bytes=None if pss is None else _nonnegative_int(pss),
            virtual_bytes=_nonnegative_int(value.get("virtual_bytes")),
            uid=_nonnegative_int(value.get("uid")),
            name=str(value.get("name", ""))[:80],
            state=str(value.get("state", "?"))[:8],
            threads=max(1, _nonnegative_int(value.get("threads"))),
            command=str(value.get("command", ""))[:4096],
            anon_bytes=_nonnegative_int(value.get("anon_bytes")),
            file_bytes=_nonnegative_int(value.get("file_bytes")),
            shared_bytes=_nonnegative_int(value.get("shared_bytes")),
        )


@dataclass(frozen=True, slots=True)
class SystemMetrics:
    cpu_percent: float | None
    load_1: float
    load_5: float
    load_15: float
    logical_cpus: int
    uptime_seconds: float
    memory_total: int
    memory_available: int
    memory_free: int
    memory_buffers: int
    memory_cached: int
    memory_reclaimable: int
    memory_shared: int
    memory_active: int
    memory_inactive: int
    memory_anon: int
    memory_slab: int
    memory_page_tables: int
    memory_kernel_stack: int
    memory_dirty: int
    memory_writeback: int
    swap_total: int
    swap_free: int
    pressure: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    vm: Mapping[str, int] = field(default_factory=dict)
    per_cpu_percent: tuple[float | None, ...] = ()
    cpu_frequency_mhz: tuple[float | None, ...] = ()
    memory_huge_total: int = 0
    memory_huge_free: int = 0
    memory_huge_page_size: int = 0

    @property
    def memory_used(self) -> int:
        return max(0, self.memory_total - min(self.memory_total, self.memory_available))

    @property
    def memory_percent(self) -> float:
        if self.memory_total <= 0:
            return 0.0
        return 100.0 * self.memory_used / self.memory_total

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SystemMetrics:
        cpu = value.get("cpu_percent")
        pressure: dict[str, dict[str, float]] = {}
        raw_pressure = value.get("pressure", {})
        if isinstance(raw_pressure, Mapping):
            for resource, lines in raw_pressure.items():
                if isinstance(lines, Mapping):
                    pressure[str(resource)] = {
                        str(key): _finite_float(number) for key, number in lines.items()
                    }
        vm: dict[str, int] = {}
        raw_vm = value.get("vm", {})
        if isinstance(raw_vm, Mapping):
            vm = {str(key): _nonnegative_int(number) for key, number in raw_vm.items()}

        def optional_floats(name: str) -> tuple[float | None, ...]:
            raw = value.get(name, ())
            if not isinstance(raw, (list, tuple)):
                return ()
            result: list[float | None] = []
            for item in raw[:4096]:
                if item is None:
                    result.append(None)
                    continue
                parsed = _finite_float(item, -1.0)
                result.append(parsed if parsed >= 0.0 else None)
            return tuple(result)

        return cls(
            cpu_percent=None
            if cpu is None
            else max(0.0, min(100.0, _finite_float(cpu))),
            load_1=max(0.0, _finite_float(value.get("load_1"))),
            load_5=max(0.0, _finite_float(value.get("load_5"))),
            load_15=max(0.0, _finite_float(value.get("load_15"))),
            logical_cpus=max(1, _nonnegative_int(value.get("logical_cpus"))),
            uptime_seconds=max(0.0, _finite_float(value.get("uptime_seconds"))),
            memory_total=_nonnegative_int(value.get("memory_total")),
            memory_available=_nonnegative_int(value.get("memory_available")),
            memory_free=_nonnegative_int(value.get("memory_free")),
            memory_buffers=_nonnegative_int(value.get("memory_buffers")),
            memory_cached=_nonnegative_int(value.get("memory_cached")),
            memory_reclaimable=_nonnegative_int(value.get("memory_reclaimable")),
            memory_shared=_nonnegative_int(value.get("memory_shared")),
            memory_active=_nonnegative_int(value.get("memory_active")),
            memory_inactive=_nonnegative_int(value.get("memory_inactive")),
            memory_anon=_nonnegative_int(value.get("memory_anon")),
            memory_slab=_nonnegative_int(value.get("memory_slab")),
            memory_page_tables=_nonnegative_int(value.get("memory_page_tables")),
            memory_kernel_stack=_nonnegative_int(value.get("memory_kernel_stack")),
            memory_dirty=_nonnegative_int(value.get("memory_dirty")),
            memory_writeback=_nonnegative_int(value.get("memory_writeback")),
            swap_total=_nonnegative_int(value.get("swap_total")),
            swap_free=_nonnegative_int(value.get("swap_free")),
            pressure=pressure,
            vm=vm,
            per_cpu_percent=optional_floats("per_cpu_percent"),
            cpu_frequency_mhz=optional_floats("cpu_frequency_mhz"),
            memory_huge_total=_nonnegative_int(value.get("memory_huge_total")),
            memory_huge_free=_nonnegative_int(value.get("memory_huge_free")),
            memory_huge_page_size=_nonnegative_int(value.get("memory_huge_page_size")),
        )


@dataclass(frozen=True, slots=True)
class PaneMetrics:
    root_pid: int
    process_count: int
    cpu_cores: float
    rss_bytes: int
    proportional_bytes: int
    complete_pss: bool

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PaneMetrics:
        return cls(
            root_pid=_nonnegative_int(value.get("root_pid")),
            process_count=_nonnegative_int(value.get("process_count")),
            cpu_cores=max(0.0, _finite_float(value.get("cpu_cores"))),
            rss_bytes=_nonnegative_int(value.get("rss_bytes")),
            proportional_bytes=_nonnegative_int(value.get("proportional_bytes")),
            complete_pss=value.get("complete_pss") is True,
        )


def _thermal_dict(sensor: ThermalSensor) -> dict[str, Any]:
    return {
        "key": sensor.key,
        "chip": sensor.chip,
        "label": sensor.label,
        "source": sensor.source,
        "celsius": sensor.celsius,
        "warning_celsius": sensor.warning_celsius,
        "critical_celsius": sensor.critical_celsius,
    }


def _fan_dict(sensor: FanSensor) -> dict[str, Any]:
    return {
        "key": sensor.key,
        "chip": sensor.chip,
        "label": sensor.label,
        "source": sensor.source,
        "rpm": sensor.rpm,
    }


def _process_dict(process: ProcessMetrics) -> dict[str, Any]:
    return {
        "pid": process.pid,
        "ppid": process.ppid,
        "start_ticks": process.start_ticks,
        "cpu_ticks": process.cpu_ticks,
        "cpu_cores": process.cpu_cores,
        "rss_bytes": process.rss_bytes,
        "pss_bytes": process.pss_bytes,
        "virtual_bytes": process.virtual_bytes,
        "uid": process.uid,
        "name": process.name,
        "state": process.state,
        "threads": process.threads,
        "command": process.command,
        "anon_bytes": process.anon_bytes,
        "file_bytes": process.file_bytes,
        "shared_bytes": process.shared_bytes,
    }


def _system_dict(system: SystemMetrics) -> dict[str, Any]:
    return {
        "cpu_percent": system.cpu_percent,
        "load_1": system.load_1,
        "load_5": system.load_5,
        "load_15": system.load_15,
        "logical_cpus": system.logical_cpus,
        "uptime_seconds": system.uptime_seconds,
        "memory_total": system.memory_total,
        "memory_available": system.memory_available,
        "memory_free": system.memory_free,
        "memory_buffers": system.memory_buffers,
        "memory_cached": system.memory_cached,
        "memory_reclaimable": system.memory_reclaimable,
        "memory_shared": system.memory_shared,
        "memory_active": system.memory_active,
        "memory_inactive": system.memory_inactive,
        "memory_anon": system.memory_anon,
        "memory_slab": system.memory_slab,
        "memory_page_tables": system.memory_page_tables,
        "memory_kernel_stack": system.memory_kernel_stack,
        "memory_dirty": system.memory_dirty,
        "memory_writeback": system.memory_writeback,
        "swap_total": system.swap_total,
        "swap_free": system.swap_free,
        "pressure": {
            resource: dict(values) for resource, values in system.pressure.items()
        },
        "vm": dict(system.vm),
        "per_cpu_percent": tuple(system.per_cpu_percent),
        "cpu_frequency_mhz": tuple(system.cpu_frequency_mhz),
        "memory_huge_total": system.memory_huge_total,
        "memory_huge_free": system.memory_huge_free,
        "memory_huge_page_size": system.memory_huge_page_size,
    }


def _pane_dict(pane: PaneMetrics) -> dict[str, Any]:
    return {
        "root_pid": pane.root_pid,
        "process_count": pane.process_count,
        "cpu_cores": pane.cpu_cores,
        "rss_bytes": pane.rss_bytes,
        "proportional_bytes": pane.proportional_bytes,
        "complete_pss": pane.complete_pss,
    }


@dataclass(frozen=True, slots=True)
class Snapshot:
    sequence: int
    wall_time_ns: int
    monotonic_ns: int
    interval_ns: int
    boot_id: str
    system: SystemMetrics
    thermal: tuple[ThermalSensor, ...]
    processes: tuple[ProcessMetrics, ...]
    fans: tuple[FanSensor, ...] = ()
    schema: int = SCHEMA_VERSION
    panes: tuple[PaneMetrics, ...] = ()
    processes_total: int = 0
    processes_truncated: bool = False

    @property
    def hottest_celsius(self) -> float | None:
        return max((sensor.celsius for sensor in self.thermal), default=None)

    def pane(self, root_pid: int) -> PaneMetrics:
        """Aggregate one pane's root process and all current descendants.

        CPU is expressed in logical cores: 1.0 means the tree consumed one
        complete logical CPU during the preceding sample interval. PID start
        ticks are retained in each record so consumers can reject recycled
        identities when comparing snapshots.
        """
        root_pid = _nonnegative_int(root_pid)
        for pane in self.panes:
            if pane.root_pid == root_pid:
                return pane
        by_pid = {process.pid: process for process in self.processes}
        if root_pid <= 0 or root_pid not in by_pid:
            return PaneMetrics(root_pid, 0, 0.0, 0, 0, False)
        live, children = _process_children(self.processes)
        selected = [
            by_pid[pid]
            for pid in sorted(_descendant_pids(live, children, (root_pid,)))
        ]
        pss_values = [process.pss_bytes for process in selected]
        complete_pss = bool(selected) and all(value is not None for value in pss_values)
        proportional = sum(
            process.rss_bytes if process.pss_bytes is None else process.pss_bytes
            for process in selected
        )
        return PaneMetrics(
            root_pid=root_pid,
            process_count=len(selected),
            cpu_cores=sum(process.cpu_cores for process in selected),
            rss_bytes=sum(process.rss_bytes for process in selected),
            proportional_bytes=proportional,
            complete_pss=complete_pss,
        )

    def to_dict(self) -> dict[str, Any]:
        """Build the schema-1 wire dictionary without dataclass reflection.

        Field-by-field construction matches ``dataclasses.asdict`` exactly
        (a test pins the equivalence) while avoiding its deep recursion over
        every process record on the sampler's once-per-second publish path.
        """
        return {
            "sequence": self.sequence,
            "wall_time_ns": self.wall_time_ns,
            "monotonic_ns": self.monotonic_ns,
            "interval_ns": self.interval_ns,
            "boot_id": self.boot_id,
            "system": _system_dict(self.system),
            "thermal": tuple(_thermal_dict(sensor) for sensor in self.thermal),
            "processes": tuple(_process_dict(process) for process in self.processes),
            "fans": tuple(_fan_dict(fan) for fan in self.fans),
            "schema": self.schema,
            "panes": tuple(_pane_dict(pane) for pane in self.panes),
            "processes_total": self.processes_total,
            "processes_truncated": self.processes_truncated,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Snapshot:
        schema = _nonnegative_int(value.get("schema"))
        if schema != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported telemetry schema {schema}; expected {SCHEMA_VERSION}"
            )
        raw_system = value.get("system")
        if not isinstance(raw_system, Mapping):
            raise TypeError("telemetry snapshot has no system record")
        raw_thermal = value.get("thermal", ())
        raw_processes = value.get("processes", ())
        raw_fans = value.get("fans", ())
        raw_panes = value.get("panes", ())
        if (
            not isinstance(raw_thermal, (list, tuple))
            or not isinstance(raw_processes, (list, tuple))
            or not isinstance(raw_fans, (list, tuple))
            or not isinstance(raw_panes, (list, tuple))
        ):
            raise TypeError("telemetry snapshot arrays are malformed")
        processes = tuple(
            ProcessMetrics.from_dict(item)
            for item in raw_processes
            if isinstance(item, Mapping)
        )
        return cls(
            sequence=_nonnegative_int(value.get("sequence")),
            wall_time_ns=_nonnegative_int(value.get("wall_time_ns")),
            monotonic_ns=_nonnegative_int(value.get("monotonic_ns")),
            interval_ns=_nonnegative_int(value.get("interval_ns")),
            boot_id=str(value.get("boot_id", ""))[:128],
            system=SystemMetrics.from_dict(raw_system),
            thermal=tuple(
                ThermalSensor.from_dict(item)
                for item in raw_thermal
                if isinstance(item, Mapping)
            ),
            processes=processes,
            fans=tuple(
                FanSensor.from_dict(item)
                for item in raw_fans
                if isinstance(item, Mapping)
            ),
            panes=tuple(
                PaneMetrics.from_dict(item)
                for item in raw_panes
                if isinstance(item, Mapping)
            ),
            processes_total=max(
                len(processes), _nonnegative_int(value.get("processes_total"))
            ),
            processes_truncated=value.get("processes_truncated") is True,
            schema=schema,
        )
