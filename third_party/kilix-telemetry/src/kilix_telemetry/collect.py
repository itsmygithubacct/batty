"""One coherent Linux sampler for all Kilix telemetry consumers."""

from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .model import (
    FanSensor,
    PaneMetrics,
    ProcessMetrics,
    Snapshot,
    SystemMetrics,
    ThermalSensor,
    _descendant_pids,
    _process_children,
)

KIB = 1024
_NATURAL_PART = re.compile(r"(\d+)")
_VM_KEYS = {
    "pgfault",
    "pgmajfault",
    "pswpin",
    "pswpout",
    "oom_kill",
    "compact_stall",
}
_VM_PREFIXES = ("pgscan_", "pgsteal_", "allocstall")


def _natural_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.lower()
        for part in _NATURAL_PART.split(value)
    )


def _safe_text(value: str, limit: int = 80) -> str:
    cleaned = "".join(
        character if character.isprintable() else "?" for character in value.strip()
    )
    return cleaned[:limit]


def _read_text(path: Path, limit: int = 1 << 20) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            return stream.read(limit)
    except OSError:
        return ""


def _read_binary(path: Path, limit: int = 4096) -> bytes:
    try:
        with path.open("rb") as stream:
            return stream.read(limit)
    except OSError:
        return b""


def _number(text: str) -> float | None:
    try:
        value = float(text.strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _temperature(path: Path, *, threshold: bool = False) -> float | None:
    value = _number(_read_text(path, 128))
    if value is None:
        return None
    celsius = value / 1000.0 if abs(value) > 1000.0 else value
    minimum = 1.0 if threshold else 0.0
    if celsius <= minimum or celsius > 250.0:
        return None
    return float(f"{celsius:.3f}")


def _pretty_chip(name: str) -> str:
    clean = _safe_text(name)
    lowered = clean.lower()
    aliases = {
        "coretemp": "CPU",
        "x86_pkg_temp": "CPU",
        "acpitz": "ACPI",
        "thinkpad": "ThinkPad",
        "nvme": "NVMe",
    }
    if lowered in aliases:
        return aliases[lowered]
    if lowered.startswith("pch_"):
        return "PCH"
    if lowered.startswith("iwlwifi"):
        return "Wi-Fi"
    return clean.replace("_", " ").title() or "Sensor"


def _clean_label(label: str) -> str:
    label = re.sub(r"\s+", " ", _safe_text(label)).strip()
    return re.sub(r"Package id (\d+)", r"Package \1", label, flags=re.IGNORECASE)


def _parse_key_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, tail = line.partition(":")
        if separator:
            values[key] = tail.strip()
    return values


def _status_bytes(status: dict[str, str], key: str) -> int:
    fields = status.get(key, "").split()
    if not fields:
        return 0
    try:
        value = max(0, int(fields[0]))
    except ValueError:
        return 0
    return value * KIB if len(fields) > 1 and fields[1].lower() == "kb" else value


@dataclass(frozen=True, slots=True)
class _RawProcess:
    pid: int
    ppid: int
    start_ticks: int
    cpu_ticks: int
    child_cpu_ticks: int
    rss_bytes: int
    virtual_bytes: int
    uid: int
    name: str
    state: str
    threads: int
    command: str
    anon_bytes: int
    file_bytes: int
    shared_bytes: int


@dataclass(frozen=True, slots=True)
class _TemperatureSource:
    """Static identity of one temperature input; only its value changes."""

    input_path: Path
    key: str
    chip: str
    label: str
    source: str
    warning_celsius: float | None
    critical_celsius: float | None


@dataclass(frozen=True, slots=True)
class _FanSource:
    """Static identity of one fan tachometer input."""

    input_path: Path
    key: str
    chip: str
    label: str
    source: str


@dataclass(frozen=True, slots=True)
class _ProcessMeta:
    refreshed_ns: int
    uid: int
    name: str
    command: str
    anon_bytes: int
    file_bytes: int
    shared_bytes: int


def _parse_stat(
    pid: int, text: str, page_size: int
) -> (
    tuple[
        int,
        int,
        int,
        int,
        int,
        int,
        str,
        str,
        int,
    ]
    | None
):
    left = text.find("(")
    right = text.rfind(")")
    if left < 0 or right <= left:
        return None
    fields = text[right + 1 :].split()
    if len(fields) <= 21:
        return None
    try:
        return (
            max(0, int(fields[1])),
            max(0, int(fields[19])),
            max(0, int(fields[11]) + int(fields[12])),
            max(0, int(fields[13]) + int(fields[14])),
            max(0, int(fields[21])) * page_size,
            max(0, int(fields[20])),
            _safe_text(text[left + 1 : right], 80) or str(pid),
            _safe_text(fields[0], 8) or "?",
            max(1, int(fields[17])),
        )
    except ValueError:
        return None


class LinuxCollector:
    """Collect global and per-process data once for every Kilix consumer."""

    def __init__(
        self,
        root: str | Path = "/",
        *,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        wall_time_ns: Callable[[], int] = time.time_ns,
        pss_interval: float = 2.0,
        metadata_interval: float = 5.0,
        process_interval: float = 2.0,
        thermal_interval: float = 2.0,
    ) -> None:
        self.root = Path(root)
        self.proc = self.root / "proc"
        self.sys = self.root / "sys"
        self._monotonic_ns = monotonic_ns
        self._wall_time_ns = wall_time_ns
        self._previous_when_ns: int | None = None
        self._previous_process_when_ns: int | None = None
        self._previous_cpu: tuple[int, int] | None = None
        self._previous_per_cpu: dict[int, tuple[int, int]] = {}
        self._previous_processes: dict[int, tuple[int, int]] = {}
        self._sequence = 0
        self._pss_interval_ns = max(0, int(pss_interval * 1_000_000_000))
        self._pss_cache: dict[tuple[int, int], tuple[int, int | None]] = {}
        self._metadata_interval_ns = max(0, int(metadata_interval * 1_000_000_000))
        self._metadata_cache: dict[tuple[int, int], _ProcessMeta] = {}
        self._process_interval_ns = max(
            100_000_000, int(process_interval * 1_000_000_000)
        )
        self._process_sample_ns: int | None = None
        self._process_pss_roots: tuple[int, ...] = ()
        self._process_cache: tuple[ProcessMetrics, ...] = ()
        self._pane_cache: tuple[PaneMetrics, ...] = ()
        self._previous_pane_ticks: dict[tuple[int, int], int] = {}
        self._thermal_interval_ns = max(
            100_000_000, int(thermal_interval * 1_000_000_000)
        )
        self._thermal_sample_ns: int | None = None
        self._thermal_cache: tuple[ThermalSensor, ...] = ()
        self._fan_cache: tuple[FanSensor, ...] = ()
        self._sensor_rescan_ns = max(self._thermal_interval_ns, 60_000_000_000)
        self._sensors_scanned_ns: int | None = None
        self._rescan_sensors = False
        self._temperature_sources: tuple[_TemperatureSource, ...] = ()
        self._fan_sources: tuple[_FanSource, ...] = ()
        self._frequency_sample_ns: int | None = None
        self._frequency_cache: tuple[float | None, ...] = ()
        try:
            self._ticks_per_second = max(1, int(os.sysconf("SC_CLK_TCK")))
        except (OSError, ValueError):
            self._ticks_per_second = 100
        try:
            self._page_size = max(1, int(os.sysconf("SC_PAGE_SIZE")))
        except (OSError, ValueError):
            self._page_size = 4096
        self._uid = os.getuid()
        self._boot_id = _safe_text(
            _read_text(self.proc / "sys/kernel/random/boot_id", 256), 128
        )

    def _raw_process(self, directory: Path, now_ns: int) -> _RawProcess | None:
        try:
            pid = int(directory.name)
        except ValueError:
            return None
        stat = _parse_stat(pid, _read_text(directory / "stat", 8192), self._page_size)
        if stat is None:
            return None
        (
            ppid,
            start_ticks,
            cpu_ticks,
            child_cpu_ticks,
            rss,
            virtual,
            name,
            state,
            threads,
        ) = stat
        identity = (pid, start_ticks)
        meta = self._metadata_cache.get(identity)
        if meta is None or now_ns - meta.refreshed_ns >= self._metadata_interval_ns:
            status = _parse_key_values(_read_text(directory / "status", 65536))
            uid_fields = status.get("Uid", "0").split()
            try:
                uid = max(0, int(uid_fields[0]))
            except (IndexError, ValueError):
                uid = 0
            command = (
                _read_binary(directory / "cmdline")
                .replace(b"\0", b" ")
                .decode("utf-8", errors="replace")
                .strip()
            ) or f"[{name}]"
            meta = _ProcessMeta(
                refreshed_ns=now_ns,
                uid=uid,
                name=_safe_text(status.get("Name", name), 80) or name,
                command=_safe_text(command, 4096),
                anon_bytes=_status_bytes(status, "RssAnon"),
                file_bytes=_status_bytes(status, "RssFile"),
                shared_bytes=_status_bytes(status, "RssShmem"),
            )
            self._metadata_cache[identity] = meta
        return _RawProcess(
            pid=pid,
            ppid=ppid,
            start_ticks=start_ticks,
            cpu_ticks=cpu_ticks,
            child_cpu_ticks=child_cpu_ticks,
            rss_bytes=rss,
            virtual_bytes=virtual,
            uid=meta.uid,
            name=meta.name,
            state=state,
            threads=threads,
            command=meta.command,
            anon_bytes=meta.anon_bytes,
            file_bytes=meta.file_bytes,
            shared_bytes=meta.shared_bytes,
        )

    def _pss(self, process: _RawProcess, now_ns: int) -> int | None:
        if process.uid != self._uid:
            return None
        identity = (process.pid, process.start_ticks)
        cached = self._pss_cache.get(identity)
        if cached is not None and now_ns - cached[0] < self._pss_interval_ns:
            return cached[1]
        value: int | None = None
        for line in _read_text(
            self.proc / str(process.pid) / "smaps_rollup", 1 << 20
        ).splitlines():
            if not line.startswith("Pss:"):
                continue
            fields = line.split()
            try:
                value = max(0, int(fields[1])) * KIB
            except (IndexError, ValueError):
                value = None
            break
        self._pss_cache[identity] = (now_ns, value)
        return value

    def _processes(
        self,
        now_ns: int,
        pss_roots: tuple[int, ...],
    ) -> tuple[tuple[ProcessMetrics, ...], tuple[PaneMetrics, ...]]:
        pss_roots = tuple(sorted({pid for pid in pss_roots if pid > 0}))
        if (
            self._process_sample_ns is not None
            and now_ns - self._process_sample_ns < self._process_interval_ns
            and pss_roots == self._process_pss_roots
        ):
            return self._process_cache, self._pane_cache
        try:
            directories = sorted(
                (entry for entry in self.proc.iterdir() if entry.name.isdigit()),
                key=lambda path: int(path.name),
            )
        except OSError:
            directories = []
        raw = tuple(
            process
            for directory in directories
            if (process := self._raw_process(directory, now_ns)) is not None
        )
        elapsed_ns = (
            0
            if self._previous_process_when_ns is None
            else max(0, now_ns - self._previous_process_when_ns)
        )
        denominator = self._ticks_per_second * (elapsed_ns / 1_000_000_000)
        current = {
            process.pid: (process.start_ticks, process.cpu_ticks) for process in raw
        }
        live, children = _process_children(raw)
        by_pid = {process.pid: process for process in raw}
        trees: dict[int, tuple[int, ...]] = {}
        pss_pids: set[int] = set()
        for root_pid in pss_roots:
            selected = _descendant_pids(live, children, (root_pid,))
            trees[root_pid] = tuple(sorted(selected))
            pss_pids.update(selected)
        result: list[ProcessMetrics] = []
        for process in raw:
            previous = self._previous_processes.get(process.pid)
            cpu_cores = 0.0
            if (
                previous is not None
                and previous[0] == process.start_ticks
                and denominator > 0.0
            ):
                delta = process.cpu_ticks - previous[1]
                if delta > 0:
                    cpu_cores = delta / denominator
            result.append(
                ProcessMetrics(
                    pid=process.pid,
                    ppid=process.ppid,
                    start_ticks=process.start_ticks,
                    cpu_ticks=process.cpu_ticks,
                    cpu_cores=max(0.0, cpu_cores),
                    rss_bytes=process.rss_bytes,
                    pss_bytes=(
                        self._pss(process, now_ns) if process.pid in pss_pids else None
                    ),
                    virtual_bytes=process.virtual_bytes,
                    uid=process.uid,
                    name=process.name,
                    state=process.state,
                    threads=process.threads,
                    command=process.command,
                    anon_bytes=process.anon_bytes,
                    file_bytes=process.file_bytes,
                    shared_bytes=process.shared_bytes,
                )
            )
        metrics_by_pid = {process.pid: process for process in result}
        pane_result: list[PaneMetrics] = []
        current_pane_ticks: dict[tuple[int, int], int] = {}
        for root_pid in pss_roots:
            selected_raw = [by_pid[pid] for pid in trees[root_pid]]
            selected_metrics = [metrics_by_pid[pid] for pid in trees[root_pid]]
            root = by_pid.get(root_pid)
            cpu_cores = 0.0
            if root is not None:
                identity = (root.pid, root.start_ticks)
                tree_ticks = sum(
                    process.cpu_ticks + process.child_cpu_ticks
                    for process in selected_raw
                )
                previous_ticks = self._previous_pane_ticks.get(identity)
                if previous_ticks is not None and denominator > 0.0:
                    cpu_cores = max(0, tree_ticks - previous_ticks) / denominator
                current_pane_ticks[identity] = tree_ticks
            pss_values = [process.pss_bytes for process in selected_metrics]
            pane_result.append(
                PaneMetrics(
                    root_pid=root_pid,
                    process_count=len(selected_metrics),
                    cpu_cores=cpu_cores,
                    rss_bytes=sum(process.rss_bytes for process in selected_metrics),
                    proportional_bytes=sum(
                        process.rss_bytes
                        if process.pss_bytes is None
                        else process.pss_bytes
                        for process in selected_metrics
                    ),
                    complete_pss=bool(selected_metrics)
                    and all(value is not None for value in pss_values),
                )
            )
        self._previous_processes = current
        self._previous_pane_ticks = current_pane_ticks
        live = {(process.pid, process.start_ticks) for process in raw}
        self._pss_cache = {
            identity: cached
            for identity, cached in self._pss_cache.items()
            if identity in live
        }
        self._metadata_cache = {
            identity: meta
            for identity, meta in self._metadata_cache.items()
            if identity in live
        }
        self._process_sample_ns = now_ns
        self._previous_process_when_ns = now_ns
        self._process_pss_roots = pss_roots
        self._process_cache = tuple(result)
        self._pane_cache = tuple(pane_result)
        return self._process_cache, self._pane_cache

    @staticmethod
    def _cpu_percent(
        current: tuple[int, int], previous: tuple[int, int] | None
    ) -> float | None:
        total, idle = current
        if previous is None or total <= previous[0]:
            return None
        total_delta = total - previous[0]
        idle_delta = max(0, idle - previous[1])
        percent = 100.0 * max(0, total_delta - idle_delta) / total_delta
        return max(0.0, min(100.0, percent))

    def _cpu(self) -> tuple[float | None, int, tuple[float | None, ...]]:
        aggregate = (0, 0)
        per_cpu: dict[int, tuple[int, int]] = {}
        for line in _read_text(self.proc / "stat").splitlines():
            fields = line.split()
            if not fields or not fields[0].startswith("cpu"):
                continue
            try:
                values = [max(0, int(value)) for value in fields[1:]]
            except ValueError:
                continue
            if len(values) < 4:
                continue
            total = sum(values)
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            if fields[0] == "cpu":
                aggregate = (total, idle)
            elif fields[0][3:].isdigit():
                per_cpu[int(fields[0][3:])] = (total, idle)
        percent = self._cpu_percent(aggregate, self._previous_cpu)
        self._previous_cpu = aggregate
        logical = max(
            1,
            (max(per_cpu) + 1) if per_cpu else (os.cpu_count() or 1),
        )
        per_percent = tuple(
            self._cpu_percent(per_cpu[index], self._previous_per_cpu.get(index))
            if index in per_cpu
            else None
            for index in range(logical)
        )
        self._previous_per_cpu = per_cpu
        return percent, logical, per_percent

    def _cpu_frequency(
        self, now_ns: int, logical_cpus: int
    ) -> tuple[float | None, ...]:
        if (
            self._frequency_sample_ns is not None
            and now_ns - self._frequency_sample_ns < self._thermal_interval_ns
            and len(self._frequency_cache) == logical_cpus
        ):
            return self._frequency_cache
        values: list[float | None] = [None] * logical_cpus
        current_cpu: int | None = None
        for line in _read_text(self.proc / "cpuinfo", 8 << 20).splitlines():
            key, separator, raw = line.partition(":")
            if not separator:
                continue
            key = key.strip().lower()
            if key == "processor":
                try:
                    current_cpu = int(raw.strip())
                except ValueError:
                    current_cpu = None
            elif key == "cpu mhz" and current_cpu is not None:
                mhz = _number(raw)
                if (
                    mhz is not None
                    and 0.0 < mhz < 1_000_000.0
                    and 0 <= current_cpu < len(values)
                ):
                    values[current_cpu] = float(f"{mhz:.3f}")
        for index, value in enumerate(values):
            if value is not None:
                continue
            khz = _number(
                _read_text(
                    self.sys
                    / "devices/system/cpu"
                    / f"cpu{index}/cpufreq/scaling_cur_freq",
                    128,
                )
            )
            if khz is not None and 0.0 < khz < 1_000_000_000.0:
                values[index] = float(f"{khz / 1000.0:.3f}")
        self._frequency_sample_ns = now_ns
        self._frequency_cache = tuple(values)
        return self._frequency_cache

    def _loads(self) -> tuple[float, float, float]:
        fields = _read_text(self.proc / "loadavg", 256).split()
        try:
            return tuple(max(0.0, float(fields[index])) for index in range(3))  # type: ignore[return-value]
        except (IndexError, ValueError):
            return 0.0, 0.0, 0.0

    def _uptime(self) -> float:
        fields = _read_text(self.proc / "uptime", 128).split()
        try:
            return max(0.0, float(fields[0]))
        except (IndexError, ValueError):
            return 0.0

    def _meminfo(self) -> dict[str, int]:
        values: dict[str, int] = {}
        for key, tail in _parse_key_values(_read_text(self.proc / "meminfo")).items():
            fields = tail.split()
            if not fields:
                continue
            try:
                amount = max(0, int(fields[0]))
            except ValueError:
                continue
            values[key] = (
                amount * KIB
                if (len(fields) > 1 and fields[1].lower() == "kb")
                else amount
            )
        total = values.get("MemTotal", 0)
        free = min(total, values.get("MemFree", 0))
        fallback = (
            free
            + values.get("Buffers", 0)
            + values.get("Cached", 0)
            + values.get("SReclaimable", 0)
            - values.get("Shmem", 0)
        )
        values["MemAvailable"] = max(
            free, min(total, values.get("MemAvailable", fallback))
        )
        return values

    def _pressure(self) -> dict[str, dict[str, float]]:
        result: dict[str, dict[str, float]] = {}
        for resource in ("cpu", "memory", "io"):
            values: dict[str, float] = {}
            for line in _read_text(self.proc / "pressure" / resource).splitlines():
                fields = line.split()
                if not fields:
                    continue
                for item in fields[1:]:
                    key, separator, raw = item.partition("=")
                    if not separator:
                        continue
                    try:
                        value = max(0.0, float(raw))
                    except ValueError:
                        continue
                    values[f"{fields[0]}_{key}"] = value
            if values:
                result[resource] = values
        return result

    def _vm(self) -> dict[str, int]:
        values: dict[str, int] = {}
        for line in _read_text(self.proc / "vmstat").splitlines():
            fields = line.split()
            if len(fields) != 2:
                continue
            key = fields[0]
            if key not in _VM_KEYS and not key.startswith(_VM_PREFIXES):
                continue
            try:
                values[key] = max(0, int(fields[1]))
            except ValueError:
                continue
        return values

    def _discover_sensors(self) -> None:
        """Scan sysfs for sensor topology: paths, names, labels, thresholds.

        Everything gathered here is a hardware constant - only the *_input
        values change between refreshes - so the result is cached and
        re-scanned on a slow cadence or when an input file disappears.
        """
        temperatures: list[_TemperatureSource] = []
        fans: list[_FanSource] = []
        thermal_root = self.sys / "class/thermal"
        for zone in sorted(
            thermal_root.glob("thermal_zone*"), key=lambda path: _natural_key(path.name)
        ):
            # A zone directory without a temp input can never produce a
            # reading; registering it anyway would flag a topology rescan
            # on every refresh when its absence is noticed.
            if not (zone / "temp").exists():
                continue
            zone_type = _safe_text(_read_text(zone / "type", 256)) or zone.name
            warning: float | None = None
            critical: float | None = None
            for type_path in sorted(zone.glob("trip_point_*_type")):
                kind = _read_text(type_path, 128).strip().lower()
                value = _temperature(
                    type_path.with_name(type_path.name.replace("_type", "_temp")),
                    threshold=True,
                )
                if value is None:
                    continue
                if kind == "critical":
                    critical = value if critical is None else min(critical, value)
                elif kind in {"hot", "active", "passive"}:
                    warning = value if warning is None else min(warning, value)
            index = zone.name.removeprefix("thermal_zone")
            temperatures.append(
                _TemperatureSource(
                    input_path=zone / "temp",
                    key=f"zone:{index}:{zone_type}",
                    chip=_pretty_chip(zone_type),
                    label=f"zone {index}",
                    source="thermal-zone",
                    warning_celsius=warning,
                    critical_celsius=critical,
                )
            )
        hwmon_root = self.sys / "class/hwmon"
        for hwmon in sorted(
            hwmon_root.glob("hwmon*"), key=lambda path: _natural_key(path.name)
        ):
            raw_chip = _safe_text(_read_text(hwmon / "name", 256)) or hwmon.name
            for input_path in sorted(
                hwmon.glob("temp*_input"), key=lambda path: _natural_key(path.name)
            ):
                prefix = input_path.name.removesuffix("_input")
                label = _clean_label(
                    _read_text(hwmon / f"{prefix}_label", 256) or prefix
                )
                temperatures.append(
                    _TemperatureSource(
                        input_path=input_path,
                        key=f"hwmon:{hwmon.name}:{raw_chip}:{prefix}",
                        chip=_pretty_chip(raw_chip),
                        label=label,
                        source=hwmon.name,
                        warning_celsius=_temperature(
                            hwmon / f"{prefix}_max", threshold=True
                        ),
                        critical_celsius=_temperature(
                            hwmon / f"{prefix}_crit", threshold=True
                        ),
                    )
                )
            for input_path in sorted(
                hwmon.glob("fan*_input"), key=lambda path: _natural_key(path.name)
            ):
                prefix = input_path.name.removesuffix("_input")
                label = _clean_label(
                    _read_text(hwmon / f"{prefix}_label", 256) or prefix
                )
                fans.append(
                    _FanSource(
                        input_path=input_path,
                        key=f"fan:{hwmon.name}:{raw_chip}:{prefix}",
                        chip=_pretty_chip(raw_chip),
                        label=label,
                        source=hwmon.name,
                    )
                )
        self._temperature_sources = tuple(temperatures)
        self._fan_sources = tuple(fans)

    def _thermal(self, now_ns: int) -> tuple[ThermalSensor, ...]:
        if (
            self._thermal_sample_ns is not None
            and now_ns - self._thermal_sample_ns < self._thermal_interval_ns
        ):
            return self._thermal_cache
        if (
            self._rescan_sensors
            or self._sensors_scanned_ns is None
            or now_ns - self._sensors_scanned_ns >= self._sensor_rescan_ns
        ):
            self._discover_sensors()
            self._sensors_scanned_ns = now_ns
            self._rescan_sensors = False
        sensors: list[ThermalSensor] = []
        fans: list[FanSensor] = []
        for source in self._temperature_sources:
            celsius = _temperature(source.input_path)
            if celsius is None:
                if not source.input_path.exists():
                    self._rescan_sensors = True
                continue
            sensors.append(
                ThermalSensor(
                    key=source.key,
                    chip=source.chip,
                    label=source.label,
                    source=source.source,
                    celsius=celsius,
                    warning_celsius=source.warning_celsius,
                    critical_celsius=source.critical_celsius,
                )
            )
        for fan in self._fan_sources:
            rpm = _number(_read_text(fan.input_path, 128))
            if rpm is None or rpm < 0.0 or rpm > 200_000.0:
                if rpm is None and not fan.input_path.exists():
                    self._rescan_sensors = True
                continue
            fans.append(
                FanSensor(
                    key=fan.key,
                    chip=fan.chip,
                    label=fan.label,
                    source=fan.source,
                    rpm=round(rpm),
                )
            )
        self._thermal_sample_ns = now_ns
        self._thermal_cache = tuple(sensors)
        self._fan_cache = tuple(fans)
        return self._thermal_cache

    def sample(self, *, pss_roots: tuple[int, ...] = ()) -> Snapshot:
        now_ns = self._monotonic_ns()
        wall_ns = self._wall_time_ns()
        self._sequence += 1
        interval_ns = (
            0
            if self._previous_when_ns is None
            else max(0, now_ns - self._previous_when_ns)
        )
        cpu_percent, logical_cpus, per_cpu_percent = self._cpu()
        loads = self._loads()
        memory = self._meminfo()
        processes, panes = self._processes(now_ns, pss_roots)
        thermal = self._thermal(now_ns)
        snapshot = Snapshot(
            sequence=self._sequence,
            wall_time_ns=wall_ns,
            monotonic_ns=now_ns,
            interval_ns=interval_ns,
            boot_id=self._boot_id,
            system=SystemMetrics(
                cpu_percent=cpu_percent,
                load_1=loads[0],
                load_5=loads[1],
                load_15=loads[2],
                logical_cpus=logical_cpus,
                uptime_seconds=self._uptime(),
                memory_total=memory.get("MemTotal", 0),
                memory_available=memory.get("MemAvailable", 0),
                memory_free=memory.get("MemFree", 0),
                memory_buffers=memory.get("Buffers", 0),
                memory_cached=memory.get("Cached", 0),
                memory_reclaimable=memory.get("SReclaimable", 0),
                memory_shared=memory.get("Shmem", 0),
                memory_active=memory.get("Active", 0),
                memory_inactive=memory.get("Inactive", 0),
                memory_anon=memory.get("AnonPages", 0),
                memory_slab=memory.get("Slab", 0),
                memory_page_tables=memory.get("PageTables", 0),
                memory_kernel_stack=memory.get("KernelStack", 0),
                memory_dirty=memory.get("Dirty", 0),
                memory_writeback=memory.get("Writeback", 0),
                swap_total=memory.get("SwapTotal", 0),
                swap_free=memory.get("SwapFree", 0),
                pressure=self._pressure(),
                vm=self._vm(),
                per_cpu_percent=per_cpu_percent,
                cpu_frequency_mhz=self._cpu_frequency(now_ns, logical_cpus),
                memory_huge_total=memory.get("HugePages_Total", 0),
                memory_huge_free=memory.get("HugePages_Free", 0),
                memory_huge_page_size=memory.get("Hugepagesize", 0),
            ),
            thermal=thermal,
            processes=processes,
            fans=self._fan_cache,
            panes=panes,
            processes_total=len(processes),
        )
        self._previous_when_ns = now_ns
        return snapshot
