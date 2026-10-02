"""Typed Python bindings for bounded, crash-safe ``kilix-state`` records."""

from ._native import (
    IncompatibleLibraryError,
    KilixStateError,
    KilixStateLibrary,
    LibraryNotFoundError,
    default_library,
)
from .store import (
    DEFAULT_MAX_PAYLOAD,
    MAX_PAYLOAD,
    BufferTooSmallError,
    CorruptStateError,
    Format,
    InvalidStateError,
    Result,
    StateIOError,
    StateNotFoundError,
    StateOperationError,
    StateTooLargeError,
    Store,
    StoreClosedError,
    UnsafeStatePathError,
    crc32,
    result_name,
)


__version__ = "0.1.0"
KILIX_STATE_ABI = (0, 5)


__all__ = [
    "DEFAULT_MAX_PAYLOAD",
    "KILIX_STATE_ABI",
    "MAX_PAYLOAD",
    "BufferTooSmallError",
    "CorruptStateError",
    "Format",
    "IncompatibleLibraryError",
    "InvalidStateError",
    "KilixStateError",
    "KilixStateLibrary",
    "LibraryNotFoundError",
    "Result",
    "StateIOError",
    "StateNotFoundError",
    "StateOperationError",
    "StateTooLargeError",
    "Store",
    "StoreClosedError",
    "UnsafeStatePathError",
    "__version__",
    "crc32",
    "default_library",
    "result_name",
]
