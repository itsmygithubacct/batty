"""Pythonic, lifecycle-safe wrappers around the ``kilix-state`` C API."""

from __future__ import annotations

import ctypes
from enum import IntEnum
import operator
import os
from pathlib import Path
import threading
from typing import Any, NoReturn

from ._native import (
    FILENAME_CAPACITY,
    PATH_CAPACITY,
    IncompatibleLibraryError,
    KilixStateError,
    KilixStateLibrary,
    _Options,
    _Store,
    default_library,
)


DEFAULT_MAX_PAYLOAD = 64 * 1024
MAX_PAYLOAD = 16 * 1024 * 1024
MAX_LOAD_ATTEMPTS = 4


class Format(IntEnum):
    """Native on-disk formats."""

    CRC32 = 1
    RAW = 2


class Result(IntEnum):
    """Native ``kilixstate_result`` values."""

    OK = 0
    NOT_FOUND = 1
    INVALID = 2
    TOO_LARGE = 3
    BUFFER_TOO_SMALL = 4
    CORRUPT = 5
    SECURITY = 6
    IO_ERROR = 7
    NOT_INITIALIZED = 8


def _default_detail(result: Result | int) -> str:
    """Native diagnostic name, or the enum name if no library loads.

    Exceptions such as ``StateTooLargeError`` can be raised before any
    native call, so a missing or incompatible library must degrade the
    message rather than mask the original failure.
    """

    try:
        return result_name(result)
    except KilixStateError:
        name = getattr(result, "name", None)
        if name is None:
            return f"result {int(result)}"
        return name.replace("_", " ").lower()


class StateOperationError(KilixStateError):
    """Base exception for a failed native store operation."""

    def __init__(
        self, result: Result, operation: str, detail: str | None = None
    ) -> None:
        self.result = result
        self.operation = operation
        self.detail = detail or _default_detail(result)
        super().__init__(f"{operation} failed: {self.detail}")


class StateNotFoundError(StateOperationError):
    """Raised when a requested state record does not exist."""


class InvalidStateError(StateOperationError):
    """Raised for invalid options or payload arguments."""


class StateTooLargeError(StateOperationError):
    """Raised when a record exceeds the configured payload bound."""


class BufferTooSmallError(StateOperationError):
    """Raised when a native destination buffer is unexpectedly too small."""


class CorruptStateError(StateOperationError):
    """Raised when a record is truncated, malformed, or fails its checksum."""


class UnsafeStatePathError(StateOperationError):
    """Raised when ownership, type, or symlink checks reject a path."""


class StateIOError(StateOperationError):
    """Raised when a native filesystem operation fails."""


class StoreClosedError(StateOperationError):
    """Raised when an operation targets a closed store."""


_ERROR_TYPES: dict[Result, type[StateOperationError]] = {
    Result.NOT_FOUND: StateNotFoundError,
    Result.INVALID: InvalidStateError,
    Result.TOO_LARGE: StateTooLargeError,
    Result.BUFFER_TOO_SMALL: BufferTooSmallError,
    Result.CORRUPT: CorruptStateError,
    Result.SECURITY: UnsafeStatePathError,
    Result.IO_ERROR: StateIOError,
    Result.NOT_INITIALIZED: StoreClosedError,
}


def _raise_result(value: int, operation: str) -> NoReturn:
    try:
        result = Result(value)
    except ValueError as error:
        raise IncompatibleLibraryError(
            f"{operation} returned unknown kilix-state result {value}"
        ) from error
    exception = _ERROR_TYPES.get(result, StateOperationError)
    raise exception(result, operation)


def _library(value: KilixStateLibrary | None) -> KilixStateLibrary:
    if value is None:
        return default_library()
    if not isinstance(value, KilixStateLibrary):
        raise TypeError("library must be a KilixStateLibrary")
    return value


def _bounded_size(value: Any) -> int:
    try:
        result = operator.index(value)
    except TypeError as error:
        raise TypeError("max_payload must be an integer") from error
    if result < 1 or result > MAX_PAYLOAD:
        raise ValueError(
            f"max_payload must be between 1 and {MAX_PAYLOAD} bytes"
        )
    return result


def _component_bytes(value: str | bytes | None, name: str) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, (str, bytes)):
        raise TypeError(f"{name} must be str, bytes, or None")
    encoded = os.fsencode(value)
    if b"\0" in encoded:
        raise ValueError(f"{name} cannot contain NUL bytes")
    return encoded


def _path_bytes(
    value: str | bytes | os.PathLike[str] | os.PathLike[bytes] | None,
    name: str,
) -> bytes | None:
    if value is None:
        return None
    try:
        encoded = os.fsencode(value)
    except TypeError as error:
        raise TypeError(f"{name} must be path-like or None") from error
    if b"\0" in encoded:
        raise ValueError(f"{name} cannot contain NUL bytes")
    return encoded


def _buffer_bytes(
    value: Any, *, maximum: int | None = None, operation: str = "save"
) -> bytes:
    if isinstance(value, bytes):
        if maximum is not None and len(value) > maximum:
            raise StateTooLargeError(Result.TOO_LARGE, operation)
        return value
    try:
        view = memoryview(value)
    except TypeError as error:
        raise TypeError("payload must support the buffer protocol") from error
    byte_view: memoryview | None = None
    try:
        if not view.c_contiguous:
            raise BufferError("payload must be C-contiguous")
        if maximum is not None and view.nbytes > maximum:
            raise StateTooLargeError(Result.TOO_LARGE, operation)
        byte_view = view.cast("B")
        return byte_view.tobytes()
    finally:
        if byte_view is not None:
            byte_view.release()
        view.release()


def _native_bytes(data: bytes) -> ctypes.c_char_p | None:
    if not data:
        return None
    return ctypes.c_char_p(data)


def result_name(
    result: Result | int, *, library: KilixStateLibrary | None = None
) -> str:
    """Return the native diagnostic name for a result value."""

    native = _library(library)
    try:
        value = operator.index(result)
    except TypeError as error:
        raise TypeError("result must be an integer or Result") from error
    name = native.raw.kilixstate_result_name(value)
    if not name:
        raise IncompatibleLibraryError(
            "kilixstate_result_name returned a null pointer")
    return name.decode("ascii", "replace")


def crc32(
    payload: Any, *, library: KilixStateLibrary | None = None
) -> int:
    """Compute the same CRC-32 used by native versioned records."""

    native = _library(library)
    data = _buffer_bytes(payload, operation="crc32")
    buffer = _native_bytes(data)
    return int(native.raw.kilixstate_crc32(buffer, len(data)))


class Store:
    """One bounded, private, crash-safe native state record.

    Methods on the same instance are serialized because the native store owns
    a stable directory descriptor whose save/load/close lifecycle must not
    race. Separate processes retain atomic replacement semantics; concurrent
    writers are last-writer-wins rather than transactional.
    """

    __slots__ = (
        "_closed",
        "_format",
        "_library",
        "_lock",
        "_max_payload",
        "_store",
    )

    def __init__(
        self,
        app_id: str | bytes | None = None,
        filename: str | bytes | None = None,
        *,
        base_directory: (
            str | bytes | os.PathLike[str] | os.PathLike[bytes] | None
        ) = None,
        absolute_path: (
            str | bytes | os.PathLike[str] | os.PathLike[bytes] | None
        ) = None,
        max_payload: int = DEFAULT_MAX_PAYLOAD,
        format: Format | int = Format.CRC32,
        library: KilixStateLibrary | None = None,
    ) -> None:
        native = _library(library)
        payload_limit = _bounded_size(max_payload)
        try:
            record_format = Format(format)
        except (TypeError, ValueError) as error:
            raise ValueError("format must be Format.CRC32 or Format.RAW") from error

        options = _Options()
        native.raw.kilixstate_options_init(ctypes.byref(options))
        options.app_id = _component_bytes(app_id, "app_id")
        options.filename = _component_bytes(filename, "filename")
        options.base_directory = _path_bytes(
            base_directory, "base_directory")
        options.absolute_path = _path_bytes(absolute_path, "absolute_path")
        options.max_payload = payload_limit
        options.format = int(record_format)

        self._library = native
        self._store = _Store()
        self._lock = threading.RLock()
        self._closed = True
        self._max_payload = payload_limit
        self._format = record_format
        result = int(native.raw.kilixstate_store_init(
            ctypes.byref(self._store), ctypes.byref(options)))
        if result != Result.OK:
            _raise_result(result, "initialize store")
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def max_payload(self) -> int:
        return self._max_payload

    @property
    def format(self) -> Format:
        return self._format

    @property
    def library(self) -> KilixStateLibrary:
        return self._library

    def _require_open(self, operation: str) -> None:
        if self._closed:
            raise StoreClosedError(Result.NOT_INITIALIZED, operation)

    def _path_unlocked(self) -> Path:
        self._require_open("resolve path")
        capacity = PATH_CAPACITY + FILENAME_CAPACITY + 2
        buffer = ctypes.create_string_buffer(capacity)
        result = int(self._library.raw.kilixstate_store_path(
            ctypes.byref(self._store), buffer, capacity))
        if result != Result.OK:
            _raise_result(result, "resolve path")
        return Path(os.fsdecode(buffer.value))

    @property
    def path(self) -> Path:
        with self._lock:
            return self._path_unlocked()

    @property
    def directory(self) -> Path:
        return self.path.parent

    def save(self, payload: Any) -> None:
        """Atomically replace the record with bytes from a buffer object."""

        data = _buffer_bytes(
            payload, maximum=self._max_payload, operation="save")
        buffer = _native_bytes(data)
        with self._lock:
            self._require_open("save")
            result = int(self._library.raw.kilixstate_save(
                ctypes.byref(self._store), buffer, len(data)))
            if result != Result.OK:
                _raise_result(result, "save")

    def load(self) -> bytes:
        """Load and verify the record, returning its payload bytes."""

        with self._lock:
            self._require_open("load")
            capacity = min(self._max_payload, DEFAULT_MAX_PAYLOAD)
            for _attempt in range(MAX_LOAD_ATTEMPTS):
                buffer = (ctypes.c_uint8 * capacity)()
                actual = ctypes.c_size_t()
                result = int(self._library.raw.kilixstate_load(
                    ctypes.byref(self._store), buffer, capacity,
                    ctypes.byref(actual)))
                if result == Result.OK:
                    if actual.value > capacity:
                        raise IncompatibleLibraryError(
                            "kilixstate_load reported a payload larger than "
                            "the supplied buffer"
                        )
                    return ctypes.string_at(buffer, actual.value)
                if result != Result.BUFFER_TOO_SMALL:
                    _raise_result(result, "load")
                if actual.value > self._max_payload:
                    _raise_result(Result.TOO_LARGE, "load")
                if actual.value == 0:
                    raise IncompatibleLibraryError(
                        "kilixstate_load returned BUFFER_TOO_SMALL without "
                        "a required payload size"
                    )
                capacity = actual.value
            raise StateIOError(
                Result.IO_ERROR,
                "load",
                "record size changed repeatedly during concurrent updates",
            )

    def load_or(self, default: Any) -> bytes:
        """Return ``default`` only when the record does not exist."""

        try:
            return self.load()
        except StateNotFoundError:
            return _buffer_bytes(default, operation="load default")

    def remove(self, *, missing_ok: bool = False) -> bool:
        """Remove the record and durably sync its directory.

        Returns ``True`` when a record was removed. With ``missing_ok=True``,
        an absent record returns ``False`` instead of raising.
        """

        with self._lock:
            self._require_open("remove")
            result = int(self._library.raw.kilixstate_remove(
                ctypes.byref(self._store)))
            if result == Result.OK:
                return True
            if result == Result.NOT_FOUND and missing_ok:
                return False
            _raise_result(result, "remove")

    def close(self) -> None:
        """Close the stable native directory descriptor; idempotent."""

        with self._lock:
            if self._closed:
                return
            self._library.raw.kilixstate_store_close(
                ctypes.byref(self._store))
            self._closed = True

    def __enter__(self) -> Store:
        with self._lock:
            self._require_open("enter context")
        return self

    def __exit__(self, _kind, _value, _traceback) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            if not self._closed:
                self._library.raw.kilixstate_store_close(
                    ctypes.byref(self._store))
                self._closed = True
        except Exception:
            pass

    def __repr__(self) -> str:
        with self._lock:
            if self._closed:
                return "Store(closed=True)"
            path = self._path_unlocked()
            return (
                f"Store(path={str(path)!r}, format={self._format.name}, "
                f"max_payload={self._max_payload})"
            )
