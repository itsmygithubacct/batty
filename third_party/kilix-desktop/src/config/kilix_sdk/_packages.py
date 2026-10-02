"""Load host-pinned Python packages without changing process import paths."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import Iterable


def _origin(module: ModuleType) -> Path | None:
    value = getattr(getattr(module, "__spec__", None), "origin", None)
    if not value:
        value = getattr(module, "__file__", None)
    if not value:
        return None
    try:
        return Path(value).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _comes_from(module: ModuleType, package_dir: Path) -> bool:
    origin = _origin(module)
    if origin is None:
        return False
    try:
        origin.relative_to(package_dir.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _load_from_directory(name: str, package_dir: Path) -> ModuleType:
    init = package_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        name, init, submodule_search_locations=[str(package_dir)])
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create an import specification for {name}")
    module = importlib.util.module_from_spec(spec)
    before = set(sys.modules)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        for loaded_name in set(sys.modules) - before:
            if loaded_name == name or loaded_name.startswith(name + "."):
                sys.modules.pop(loaded_name, None)
        if name not in before:
            sys.modules.pop(name, None)
        raise
    if not _comes_from(module, package_dir):
        sys.modules.pop(name, None)
        raise ImportError(f"{name} did not load from its selected host package")
    return module


def load_pinned_package(
    name: str,
    source_roots: Iterable[Path],
    unavailable_message: str,
) -> ModuleType:
    """Load the first present package root, or an installed fallback.

    A module already imported under the same global name must come from the
    selected package. Silently reusing an unrelated installation would defeat
    the host's version pin and can join incompatible Python and native ABIs.
    """

    existing = sys.modules.get(name)
    if name in sys.modules and existing is None:
        raise ImportError(f"{name} is blocked in sys.modules; {unavailable_message}")
    for source_root in source_roots:
        package_dir = source_root / name
        if not (package_dir / "__init__.py").is_file():
            continue
        if existing is not None:
            if _comes_from(existing, package_dir):
                return existing
            origin = _origin(existing)
            where = str(origin) if origin is not None else "an unknown location"
            raise ImportError(
                f"{name} is already imported from {where}, not the host-pinned "
                "package; restart the process before importing kilix_sdk"
            )
        return _load_from_directory(name, package_dir)

    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name != name:
            raise
        raise ImportError(unavailable_message) from error


__all__ = ["load_pinned_package"]
