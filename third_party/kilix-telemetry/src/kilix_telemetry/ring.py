"""Private mmap telemetry ring: one writer, many cheap read-only clients."""

from __future__ import annotations

import errno
import fcntl
import json
import mmap
import os
import stat
import struct
import time
import zlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Self

from .model import Snapshot, _descendant_pids, _process_children, _process_dict

RING_MAGIC = b"KILIXTELEMETRY\0\0"
RING_API_MAJOR = 1
RING_API_MINOR = 1
HEADER_SIZE = 4096
DEFAULT_SLOT_COUNT = 32
DEFAULT_SLOT_SIZE = 512 * 1024
MIN_SLOT_SIZE = 64 * 1024
MAX_SLOT_SIZE = 4 * 1024 * 1024
MAX_SLOT_COUNT = 256
FLAG_ZLIB = 1
_HEADER = struct.Struct("<16sIIIIIQQQQ")
_SLOT = struct.Struct("<QQQIIIIQ")
SLOT_HEADER_SIZE = 64


class TelemetryError(RuntimeError):
    pass


class RingUnavailable(TelemetryError):
    pass


class RingBusy(TelemetryError):
    pass


@dataclass(frozen=True, slots=True)
class TelemetryPaths:
    directory: Path
    ring: Path
    lock: Path
    panes: Path


def _absolute(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise TelemetryError(f"telemetry runtime path must be absolute: {path}")
    return path


def resolve_paths(runtime: str | Path | None = None) -> TelemetryPaths:
    if runtime is None:
        configured = os.environ.get("KILIX_TELEMETRY_RUNTIME")
        if configured:
            directory = _absolute(configured)
        elif xdg := os.environ.get("XDG_RUNTIME_DIR"):
            directory = _absolute(xdg) / "kilix" / "telemetry"
        else:
            directory = Path(f"/tmp/kilix-{os.getuid()}/telemetry")
    else:
        directory = _absolute(runtime)
    return TelemetryPaths(
        directory=directory,
        ring=directory / "telemetry-v1.ring",
        lock=directory / "telemetry-v1.lock",
        panes=directory / "telemetry-v1-panes.json",
    )


def _ensure_private_directory(path: Path) -> None:
    current = Path(path.root)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise TelemetryError(f"unsafe telemetry runtime component: {current}")
    info = path.stat()
    if info.st_uid != os.getuid():
        raise TelemetryError(f"telemetry runtime is not owned by this user: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        try:
            path.chmod(0o700)
        except OSError as error:
            raise TelemetryError(f"telemetry runtime is not private: {path}") from error


def _secure_open(path: Path, flags: int, mode: int = 0o600) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags | nofollow | cloexec, mode)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise TelemetryError(f"unsafe telemetry file: {path}")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.fchmod(fd, 0o600)
        return fd
    except Exception:
        os.close(fd)
        raise


def _slot_parameters(slot_count: int, slot_size: int) -> tuple[int, int]:
    slot_count = int(slot_count)
    slot_size = int(slot_size)
    if not 2 <= slot_count <= MAX_SLOT_COUNT:
        raise ValueError(f"slot_count must be between 2 and {MAX_SLOT_COUNT}")
    if not MIN_SLOT_SIZE <= slot_size <= MAX_SLOT_SIZE:
        raise ValueError(
            f"slot_size must be between {MIN_SLOT_SIZE} and {MAX_SLOT_SIZE}"
        )
    return slot_count, slot_size


class DaemonLock:
    def __init__(self, paths: TelemetryPaths) -> None:
        self.paths = paths
        self.fd = -1

    def acquire(self) -> None:
        _ensure_private_directory(self.paths.directory)
        fd = _secure_open(self.paths.lock, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise RingBusy("another kilix-telemetry daemon is running") from error
            raise
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        self.fd = fd

    def close(self) -> None:
        if self.fd >= 0:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = -1

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def daemon_running(paths: TelemetryPaths | None = None) -> bool:
    """Return whether a process currently owns the singleton writer lock."""
    paths = paths or resolve_paths()
    try:
        fd = _secure_open(paths.lock, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                return True
            raise
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


class RingWriter:
    def __init__(
        self,
        paths: TelemetryPaths | None = None,
        *,
        slot_count: int = DEFAULT_SLOT_COUNT,
        slot_size: int = DEFAULT_SLOT_SIZE,
    ) -> None:
        self.paths = paths or resolve_paths()
        self.slot_count, self.slot_size = _slot_parameters(slot_count, slot_size)
        _ensure_private_directory(self.paths.directory)
        self.size = HEADER_SIZE + self.slot_count * self.slot_size
        fd = _secure_open(self.paths.ring, os.O_CREAT | os.O_RDWR)
        try:
            if os.fstat(fd).st_size != self.size:
                os.close(fd)
                fd = -1
                fd = self._replace_ring()
            self.fd = fd
            self.mapping = mmap.mmap(fd, self.size, access=mmap.ACCESS_WRITE)
        except Exception:
            if fd >= 0:
                os.close(fd)
            raise
        self.started_ns = time.monotonic_ns()
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        try:
            self._write_header(latest=0, heartbeat=self.started_ns)
        finally:
            fcntl.flock(self.fd, fcntl.LOCK_UN)

    def _replace_ring(self) -> int:
        """Swap in a resized ring file without truncating the published one.

        Shrinking the live file in place would deliver SIGBUS to any reader
        whose mapping extends past the new end of file. Building the resized
        ring beside it and renaming it over the path keeps the inode a reader
        has mapped intact; readers pick up the new file when they next open
        or revalidate the path.
        """
        temporary = self.paths.ring.with_name(self.paths.ring.name + ".next")
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        fd = _secure_open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        try:
            os.ftruncate(fd, self.size)
            os.rename(temporary, self.paths.ring)
        except Exception:
            os.close(fd)
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return fd

    def _write_header(self, *, latest: int, heartbeat: int) -> None:
        _HEADER.pack_into(
            self.mapping,
            0,
            RING_MAGIC,
            RING_API_MAJOR,
            RING_API_MINOR,
            HEADER_SIZE,
            self.slot_count,
            self.slot_size,
            max(0, int(latest)),
            os.getpid(),
            self.started_ns,
            max(0, int(heartbeat)),
        )

    @staticmethod
    def _pack(raw: bytes) -> tuple[bytes, int, int]:
        compressed = zlib.compress(raw, level=1)
        if len(compressed) < len(raw):
            return compressed, FLAG_ZLIB, len(raw)
        return raw, 0, len(raw)

    @staticmethod
    def _encode(snapshot: Snapshot) -> tuple[bytes, int, int]:
        # Key order is not part of the record contract: readers parse the
        # JSON into dictionaries and the CRC covers integrity, so sorting
        # every key on the publish path would buy nothing.
        raw = json.dumps(
            snapshot.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return RingWriter._pack(raw)

    @staticmethod
    def _pane_process_ids(snapshot: Snapshot) -> set[int]:
        roots = {pane.root_pid for pane in snapshot.panes if pane.root_pid > 0}
        live, children = _process_children(snapshot.processes)
        return _descendant_pids(live, children, roots)

    def _fit(self, snapshot: Snapshot, capacity: int) -> tuple[bytes, int, int]:
        encoded = self._encode(snapshot)
        if len(encoded[0]) <= capacity:
            return encoded

        total = max(len(snapshot.processes), snapshot.processes_total)
        compact_processes = tuple(
            replace(process, command=process.command[:512])
            for process in snapshot.processes
        )
        compact = replace(
            snapshot,
            processes=compact_processes,
            processes_total=total,
            processes_truncated=True,
        )
        encoded = self._encode(compact)
        if len(encoded[0]) <= capacity:
            return encoded

        pane_pids = self._pane_process_ids(compact)
        prioritized = sorted(
            compact.processes,
            key=lambda process: (
                process.pid not in pane_pids,
                -process.cpu_cores,
                -process.rss_bytes,
                process.pid,
            ),
        )
        best = self._fit_processes(compact, prioritized, capacity)
        if best is None:
            raise TelemetryError(
                "telemetry snapshot metadata exceeds the ring slot capacity"
            )
        return best

    @classmethod
    def _fit_processes(
        cls, compact: Snapshot, prioritized: list, capacity: int
    ) -> tuple[bytes, int, int] | None:
        """Binary-search the retained process count with one serialization.

        The snapshot skeleton and each process record are serialized once;
        every probe then only joins precomputed fragments and compresses the
        result, instead of re-running the whole dict-and-dump pipeline per
        probe on the tick where the table is largest. The assembled bytes are
        identical to encoding the candidate snapshot directly (a test pins
        this), because JSON escapes any quote inside string values, so the
        empty processes array below is unambiguous.
        """
        skeleton = json.dumps(
            replace(compact, processes=()).to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        prefix, marker, suffix = skeleton.partition('"processes":[]')
        if not marker:  # pragma: no cover - to_dict always emits the array
            raise TelemetryError("telemetry snapshot skeleton is malformed")
        indexed = [
            (
                process.pid,
                json.dumps(
                    _process_dict(process), ensure_ascii=False, separators=(",", ":")
                ),
            )
            for process in prioritized
        ]
        low = 0
        high = len(indexed)
        best: tuple[bytes, int, int] | None = None
        while low <= high:
            count = (low + high) // 2
            body = ",".join(
                fragment
                for _, fragment in sorted(indexed[:count], key=lambda item: item[0])
            )
            candidate = cls._pack(
                f'{prefix}"processes":[{body}]{suffix}'.encode("utf-8")
            )
            if len(candidate[0]) <= capacity:
                best = candidate
                low = count + 1
            else:
                high = count - 1
        return best

    def publish(self, snapshot: Snapshot) -> None:
        sequence = max(1, int(snapshot.sequence))
        capacity = self.slot_size - SLOT_HEADER_SIZE
        payload, flags, raw_size = self._fit(snapshot, capacity)
        checksum = zlib.crc32(payload) & 0xFFFFFFFF
        offset = HEADER_SIZE + ((sequence - 1) % self.slot_count) * self.slot_size
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        try:
            _SLOT.pack_into(self.mapping, offset, 0, 0, 0, 0, 0, 0, 0, 0)
            start = offset + SLOT_HEADER_SIZE
            self.mapping[start : start + len(payload)] = payload
            _SLOT.pack_into(
                self.mapping,
                offset,
                sequence,
                snapshot.monotonic_ns,
                snapshot.wall_time_ns,
                flags,
                len(payload),
                raw_size,
                checksum,
                sequence,
            )
            self._write_header(
                latest=sequence,
                heartbeat=max(time.monotonic_ns(), snapshot.monotonic_ns),
            )
        finally:
            fcntl.flock(self.fd, fcntl.LOCK_UN)

    def close(self) -> None:
        mapping = getattr(self, "mapping", None)
        if mapping is not None:
            mapping.close()
            self.mapping = None  # type: ignore[assignment]
        fd = getattr(self, "fd", -1)
        if fd >= 0:
            os.close(fd)
            self.fd = -1

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class RingReader:
    def __init__(self, paths: TelemetryPaths | None = None) -> None:
        self.paths = paths or resolve_paths()
        try:
            self.fd = _secure_open(self.paths.ring, os.O_RDONLY)
        except FileNotFoundError as error:
            raise RingUnavailable("telemetry ring does not exist") from error
        self.size = os.fstat(self.fd).st_size
        if self.size < HEADER_SIZE:
            os.close(self.fd)
            raise RingUnavailable("telemetry ring is truncated")
        self.mapping = mmap.mmap(self.fd, self.size, access=mmap.ACCESS_READ)
        try:
            self.slot_count, self.slot_size = self._header()[1:3]
        except Exception:
            self.close()
            raise

    def _header(self) -> tuple[int, int, int, int]:
        try:
            (
                magic,
                major,
                _minor,
                header_size,
                slot_count,
                slot_size,
                latest,
                _writer_pid,
                _started,
                heartbeat,
            ) = _HEADER.unpack_from(self.mapping, 0)
        except (ValueError, struct.error) as error:
            raise RingUnavailable("telemetry ring header is unreadable") from error
        if magic != RING_MAGIC or major != RING_API_MAJOR:
            raise RingUnavailable("telemetry ring has an incompatible format")
        if header_size != HEADER_SIZE:
            raise RingUnavailable("telemetry ring header size is incompatible")
        _slot_parameters(slot_count, slot_size)
        if HEADER_SIZE + slot_count * slot_size != self.size:
            raise RingUnavailable("telemetry ring size does not match its header")
        return latest, slot_count, slot_size, heartbeat

    def _copy_slot(self, expected: int) -> tuple[tuple[int, ...], bytes] | None:
        """Copy one slot header and payload; the caller holds the shared lock.

        Only the cheap consistency checks and the byte copy happen here so the
        lock, which the writer must take exclusively to publish, is held for
        as short a window as possible.
        """
        if expected <= 0:
            return None
        offset = HEADER_SIZE + ((expected - 1) % self.slot_count) * self.slot_size
        try:
            header = _SLOT.unpack_from(self.mapping, offset)
        except struct.error:
            return None
        sequence, _, _, flags, payload_size, raw_size, _, tail_sequence = header
        capacity = self.slot_size - SLOT_HEADER_SIZE
        if (
            sequence != expected
            or tail_sequence != expected
            or payload_size <= 0
            or payload_size > capacity
            or raw_size <= 0
            or raw_size > 16 * MAX_SLOT_SIZE
            or flags & ~FLAG_ZLIB
        ):
            return None
        start = offset + SLOT_HEADER_SIZE
        return header, bytes(self.mapping[start : start + payload_size])

    @staticmethod
    def _parse_slot(header: tuple[int, ...], payload: bytes) -> Snapshot | None:
        """Validate and decode one copied slot outside any ring lock."""
        (
            sequence,
            monotonic_ns,
            wall_time_ns,
            flags,
            _payload_size,
            raw_size,
            checksum,
            _tail_sequence,
        ) = header
        if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
            return None
        try:
            raw = zlib.decompress(payload) if flags & FLAG_ZLIB else payload
            if len(raw) != raw_size:
                return None
            decoded: Any = json.loads(raw)
            if not isinstance(decoded, dict):
                return None
            snapshot = Snapshot.from_dict(decoded)
        except (ValueError, TypeError, zlib.error, UnicodeDecodeError):
            return None
        if (
            snapshot.sequence != sequence
            or snapshot.monotonic_ns != monotonic_ns
            or snapshot.wall_time_ns != wall_time_ns
        ):
            return None
        return snapshot

    def latest(self, *, max_age: float | None = 5.0) -> Snapshot | None:
        fcntl.flock(self.fd, fcntl.LOCK_SH)
        try:
            latest, _, _, _ = self._header()
            copied = self._copy_slot(latest)
        finally:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        snapshot = None if copied is None else self._parse_slot(*copied)
        if snapshot is None or max_age is None:
            return snapshot
        age_ns = time.monotonic_ns() - snapshot.monotonic_ns
        if age_ns < 0 or age_ns > int(max_age * 1_000_000_000):
            return None
        return snapshot

    def history(
        self,
        limit: int | None = None,
        *,
        max_age: float | None = None,
    ) -> tuple[Snapshot, ...]:
        requested = (
            self.slot_count
            if limit is None
            else max(0, min(self.slot_count, int(limit)))
        )
        fcntl.flock(self.fd, fcntl.LOCK_SH)
        try:
            latest, _, _, _ = self._header()
            copies = [
                copied
                for expected in range(latest, max(0, latest - requested), -1)
                if (copied := self._copy_slot(expected)) is not None
            ]
        finally:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        found = [
            snapshot
            for copied in copies
            if (snapshot := self._parse_slot(*copied)) is not None
        ]
        found.reverse()
        if max_age is not None:
            cutoff = time.monotonic_ns() - int(max_age * 1_000_000_000)
            found = [sample for sample in found if sample.monotonic_ns >= cutoff]
        return tuple(found)

    def close(self) -> None:
        mapping = getattr(self, "mapping", None)
        if mapping is not None:
            mapping.close()
            self.mapping = None  # type: ignore[assignment]
        fd = getattr(self, "fd", -1)
        if fd >= 0:
            os.close(fd)
            self.fd = -1

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
