"""Small locked registry telling the sampler which pane trees need PSS."""

from __future__ import annotations

import fcntl
import json
import os
import time
from typing import Any

from .ring import TelemetryPaths, _ensure_private_directory, _secure_open

REGISTRY_SCHEMA = 1
MAX_OWNERS = 64
MAX_ROOTS_PER_OWNER = 512
MAX_REGISTRY_BYTES = 64 * 1024
DEFAULT_STALE_SECONDS = 15.0


def _decode(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"schema": REGISTRY_SCHEMA, "owners": {}}
    if not isinstance(value, dict) or value.get("schema") != REGISTRY_SCHEMA:
        return {"schema": REGISTRY_SCHEMA, "owners": {}}
    if not isinstance(value.get("owners"), dict):
        value["owners"] = {}
    return value


def _clean_owners(
    value: dict[str, Any],
    *,
    now_ns: int,
    stale_ns: int,
) -> dict[str, dict[str, Any]]:
    owners: dict[str, dict[str, Any]] = {}
    raw_owners = value.get("owners", {})
    if not isinstance(raw_owners, dict):
        return owners
    for raw_owner, record in list(raw_owners.items())[:MAX_OWNERS]:
        if not isinstance(record, dict):
            continue
        try:
            owner = int(raw_owner)
            updated = int(record.get("updated_ns", 0))
        except (TypeError, ValueError):
            continue
        if owner <= 0 or updated <= 0 or now_ns - updated > stale_ns:
            continue
        raw_roots = record.get("roots", ())
        if not isinstance(raw_roots, list):
            continue
        roots = sorted(
            {
                pid
                for item in raw_roots[:MAX_ROOTS_PER_OWNER]
                if (pid := _positive_pid(item)) is not None
            }
        )
        owners[str(owner)] = {"updated_ns": updated, "roots": roots}
    return owners


def _positive_pid(value: object) -> int | None:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


class PaneRegistry:
    def __init__(
        self,
        paths: TelemetryPaths,
        *,
        stale_seconds: float = DEFAULT_STALE_SECONDS,
    ) -> None:
        self.paths = paths
        self.stale_ns = max(1, int(stale_seconds * 1_000_000_000))

    def update(self, owner_pid: int, roots: list[int] | tuple[int, ...]) -> None:
        owner = _positive_pid(owner_pid)
        if owner is None:
            raise ValueError("pane registry owner must be a positive PID")
        clean_roots = sorted(
            {
                pid
                for item in roots[:MAX_ROOTS_PER_OWNER]
                if (pid := _positive_pid(item)) is not None
            }
        )
        _ensure_private_directory(self.paths.directory)
        fd = _secure_open(self.paths.panes, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            now_ns = time.monotonic_ns()
            os.lseek(fd, 0, os.SEEK_SET)
            value = _decode(os.read(fd, MAX_REGISTRY_BYTES + 1))
            owners = _clean_owners(value, now_ns=now_ns, stale_ns=self.stale_ns)
            owners[str(owner)] = {
                "updated_ns": now_ns,
                "roots": clean_roots,
            }
            encoded = json.dumps(
                {"schema": REGISTRY_SCHEMA, "owners": owners},
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            if len(encoded) > MAX_REGISTRY_BYTES:
                raise ValueError("pane registry is too large")
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, encoded)
            os.ftruncate(fd, len(encoded))
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def roots(self) -> tuple[int, ...]:
        try:
            fd = _secure_open(self.paths.panes, os.O_RDONLY)
        except FileNotFoundError:
            return ()
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            value = _decode(os.read(fd, MAX_REGISTRY_BYTES + 1))
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        owners = _clean_owners(
            value,
            now_ns=time.monotonic_ns(),
            stale_ns=self.stale_ns,
        )
        return tuple(
            sorted({pid for record in owners.values() for pid in record["roots"]})
        )
