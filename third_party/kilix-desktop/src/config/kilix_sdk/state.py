"""Pinned ``kilix-state`` Python bindings exposed through the Kilix host SDK.

The host owns both the Python binding version and the native-library build.
External providers therefore import this module instead of discovering an
unrelated sibling checkout or a system library on their own.
"""

from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
from typing import Any

from ._packages import load_pinned_package
from . import paths


def _load_shared_package():
    root = Path(__file__).resolve().parents[2]
    return load_pinned_package(
        "kilix_state",
        (
            root / "third_party" / "kilix-state" / "python" / "src",
            root.parent / "kilix-modules" / "kilix-state" / "python" / "src",
        ),
        "kilix-state Python bindings are unavailable; initialize Kilix "
        "submodules with: git submodule update --init --recursive",
    )


_shared = _load_shared_package()

DEFAULT_MAX_PAYLOAD = _shared.DEFAULT_MAX_PAYLOAD
KILIX_STATE_ABI = _shared.KILIX_STATE_ABI
MAX_PAYLOAD = _shared.MAX_PAYLOAD
BufferTooSmallError = _shared.BufferTooSmallError
CorruptStateError = _shared.CorruptStateError
Format = _shared.Format
IncompatibleLibraryError = _shared.IncompatibleLibraryError
InvalidStateError = _shared.InvalidStateError
KilixStateError = _shared.KilixStateError
KilixStateLibrary = _shared.KilixStateLibrary
LibraryNotFoundError = _shared.LibraryNotFoundError
Result = _shared.Result
StateIOError = _shared.StateIOError
StateNotFoundError = _shared.StateNotFoundError
StateOperationError = _shared.StateOperationError
StateTooLargeError = _shared.StateTooLargeError
StoreClosedError = _shared.StoreClosedError
UnsafeStatePathError = _shared.UnsafeStatePathError
binding_version = _shared.__version__
crc32 = _shared.crc32
result_name = _shared.result_name


def native_library_path() -> Path:
    """Return the native library selected by host policy.

    An explicit environment override remains useful for packaging and tests.
    Normal launches use the private, generated host build rather than a
    mutable system installation.
    """

    override = os.environ.get("KILIX_STATE_LIBRARY")
    if override:
        return Path(os.path.abspath(os.path.expanduser(override)))
    return Path(paths.build_dir()) / "libraries" / "kilix-state" / \
        "libkilix-state.so"


@lru_cache(maxsize=8)
def _library_at(path: str) -> KilixStateLibrary:
    try:
        return KilixStateLibrary(path)
    except LibraryNotFoundError as error:
        helper = Path(paths.kilix_home()) / "scripts" / \
            "build-state-library.sh"
        raise LibraryNotFoundError(
            f"Kilix's pinned libkilix-state is unavailable at {path}; "
            f"run {helper}"
        ) from error


def default_library() -> KilixStateLibrary:
    """Load the pinned native library built for this Kilix checkout."""

    return _library_at(str(native_library_path()))


class Store(_shared.Store):
    """A ``kilix-state`` store bound to Kilix's pinned native build."""

    __slots__ = ()

    def __init__(self, *args: Any,
                 library: KilixStateLibrary | None = None,
                 **kwargs: Any) -> None:
        super().__init__(
            *args, library=library if library is not None else default_library(),
            **kwargs)


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
    "binding_version",
    "crc32",
    "default_library",
    "native_library_path",
    "result_name",
]
