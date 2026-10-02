"""Native library discovery and ``ctypes`` declarations."""

from __future__ import annotations

import ctypes
import ctypes.util
from functools import lru_cache
import os
from pathlib import Path
import sys
from typing import Iterable


PATH_CAPACITY = 4096
FILENAME_CAPACITY = 256

# Oldest native ABI whose exports and struct layout this binding supports.
SUPPORTED_ABI_MAJOR = 0
MINIMUM_ABI_MINOR = 5


class KilixStateError(Exception):
    """Base exception for binding and native state failures."""


class LibraryNotFoundError(KilixStateError):
    """Raised when ``libkilix-state`` cannot be loaded."""


class IncompatibleLibraryError(KilixStateError):
    """Raised when a loaded library does not provide the required ABI."""


class _Options(ctypes.Structure):
    _fields_ = (
        ("app_id", ctypes.c_char_p),
        ("filename", ctypes.c_char_p),
        ("base_directory", ctypes.c_char_p),
        ("absolute_path", ctypes.c_char_p),
        ("max_payload", ctypes.c_size_t),
        ("format", ctypes.c_int),
    )


class _Store(ctypes.Structure):
    _fields_ = (
        ("directory_path", ctypes.c_char * PATH_CAPACITY),
        ("filename", ctypes.c_char * FILENAME_CAPACITY),
        ("max_payload", ctypes.c_size_t),
        ("directory_fd", ctypes.c_int),
        ("format", ctypes.c_uint8),
        ("initialized", ctypes.c_bool),
    )


_STORE_SIZE = ctypes.sizeof(_Store)


def _library_names() -> tuple[str, ...]:
    if sys.platform == "win32":
        return ("kilix-state.dll", "libkilix-state.dll")
    if sys.platform == "darwin":
        return ("libkilix-state.dylib", "libkilix-state.so")
    return ("libkilix-state.so",)


def _deduplicate(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _auto_candidates() -> list[str]:
    package = Path(__file__).resolve().parent
    project = package.parents[1]
    names = _library_names()
    candidates: list[str] = []
    for name in names:
        candidates.extend((
            str(package / "_libs" / name),
            str(project.parent / "build" / name),
            str(project.parent / "kilix-state" / "build" / name),
            str(project / "kilix-state" / "build" / name),
            str(project.parent / "kilix-game-kit" / "third_party"
                / "kilix-state" / "build" / name),
        ))
    found = ctypes.util.find_library("kilix-state")
    if found:
        candidates.append(found)
    candidates.extend(names)
    return _deduplicate(candidates)


class KilixStateLibrary:
    """A loaded ``kilix-state`` library supporting the required 0.5 API.

    ``path`` can be an absolute path or a dynamic-loader name. When omitted,
    discovery checks ``KILIX_STATE_LIBRARY``, a package-local ``_libs``
    directory, conventional sibling checkouts, and finally the system loader.
    Every load is verified against the native library's reported ABI version
    and ``kilixstate_store`` size; a mismatch refuses to bind because the
    binding allocates the store struct itself and a layout skew would corrupt
    memory instead of failing cleanly.
    """

    abi_version = (SUPPORTED_ABI_MAJOR, MINIMUM_ABI_MINOR)

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        explicit_value = os.fspath(path) if path is not None else None
        if explicit_value is not None and not isinstance(explicit_value, str):
            raise TypeError("library path must resolve to str")
        environment = os.environ.get("KILIX_STATE_LIBRARY")
        if explicit_value:
            candidates = [explicit_value]
        elif environment:
            candidates = [environment]
        else:
            candidates = _auto_candidates()

        attempts: list[str] = []
        errors: list[str] = []
        library: ctypes.CDLL | None = None
        loaded_from = ""
        for candidate in _deduplicate(candidates):
            expanded = (os.path.abspath(os.path.expanduser(candidate))
                        if os.path.sep in candidate else candidate)
            attempts.append(expanded)
            try:
                library = ctypes.CDLL(expanded, use_errno=True)
            except OSError as error:
                errors.append(f"{expanded}: {error}")
                continue
            loaded_from = expanded
            break
        if library is None:
            detail = "; ".join(errors) if errors else "no candidates"
            attempted = ", ".join(attempts) if attempts else "none"
            raise LibraryNotFoundError(
                "could not load libkilix-state "
                f"(attempted {attempted}; {detail}). "
                "Set KILIX_STATE_LIBRARY to the built shared library."
            )

        self.path = loaded_from
        self.raw = library
        self.native_abi = (0, 0)
        self.store_size = 0
        self._declare_functions()
        self._verify_abi()

    def _declare(
        self, name: str, arguments: list[object], result: object
    ) -> None:
        try:
            function = getattr(self.raw, name)
        except AttributeError as error:
            raise IncompatibleLibraryError(
                f"{self.path} does not export required symbol {name}; "
                f"the kilix-state {SUPPORTED_ABI_MAJOR}."
                f"{MINIMUM_ABI_MINOR} API or a compatible later API is "
                "required"
            ) from error
        function.argtypes = arguments
        function.restype = result

    def _declare_functions(self) -> None:
        options = ctypes.POINTER(_Options)
        store = ctypes.POINTER(_Store)
        size = ctypes.c_size_t
        result = ctypes.c_int

        self._declare("kilixstate_options_init", [options], None)
        self._declare("kilixstate_store_init", [store, options], result)
        self._declare("kilixstate_store_close", [store], None)
        self._declare(
            "kilixstate_save", [store, ctypes.c_void_p, size], result)
        self._declare("kilixstate_remove", [store], result)
        self._declare(
            "kilixstate_load",
            [store, ctypes.c_void_p, size, ctypes.POINTER(size)],
            result,
        )
        self._declare(
            "kilixstate_store_path",
            [store, ctypes.POINTER(ctypes.c_char), size],
            result,
        )
        self._declare(
            "kilixstate_crc32", [ctypes.c_void_p, size], ctypes.c_uint32)
        self._declare("kilixstate_result_name", [result], ctypes.c_char_p)
        self._declare("kilixstate_abi_version", [], ctypes.c_uint32)
        self._declare("kilixstate_store_sizeof", [], size)

    def _verify_abi(self) -> None:
        version = int(self.raw.kilixstate_abi_version())
        major = version >> 16
        minor = version & 0xFFFF
        self.native_abi = (major, minor)
        if major != SUPPORTED_ABI_MAJOR or minor < MINIMUM_ABI_MINOR:
            raise IncompatibleLibraryError(
                f"{self.path} reports kilix-state ABI {major}.{minor}; "
                f"this binding requires {SUPPORTED_ABI_MAJOR}."
                f"{MINIMUM_ABI_MINOR} or a compatible later minor"
            )
        self.store_size = int(self.raw.kilixstate_store_sizeof())
        if self.store_size != _STORE_SIZE:
            raise IncompatibleLibraryError(
                f"{self.path} uses a {self.store_size}-byte "
                f"kilixstate_store but this binding was written for "
                f"{_STORE_SIZE} bytes; refusing to bind a mismatched "
                "store layout"
            )

    def __repr__(self) -> str:
        return (
            f"KilixStateLibrary(path={self.path!r}, "
            f"abi={self.abi_version!r})"
        )


@lru_cache(maxsize=1)
def default_library() -> KilixStateLibrary:
    """Load and cache the process-wide default native library."""

    return KilixStateLibrary()
