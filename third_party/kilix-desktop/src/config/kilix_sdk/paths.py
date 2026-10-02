"""Path discovery for the Kilix host checkout."""

import os
from pathlib import Path


def kilix_home() -> str:
    """Return the absolute path to the Kilix host checkout."""
    env = os.environ.get("KILIX_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return str(Path(__file__).resolve().parents[2])


def source_home() -> str:
    """Return the canonical parent directory for GPU Terminal checkouts."""
    value = os.environ.get("GPU_TERMINAL_SOURCE_HOME") or os.path.join(
        os.path.expanduser("~"), "gpu_terminal")
    return os.path.abspath(os.path.expanduser(value))


def kilix95_home() -> str:
    """Return the external Kilix-95 provider checkout path."""
    value = os.environ.get("KILIX95_DIR") or os.path.join(
        source_home(), "kilix-desktops", "kilix-95")
    return os.path.abspath(os.path.expanduser(value))


def storage_home() -> str:
    """Return the root for all Kilix-owned writable files."""
    base = os.environ.get("GPU_TERMINAL_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "gpu_terminal")
    value = os.environ.get("KILIX_STORAGE_HOME") or os.path.join(base, "kilix")
    return os.path.abspath(os.path.expanduser(value))


def _owned_dir(env_name: str, leaf: str) -> str:
    value = os.environ.get(env_name) or os.path.join(storage_home(), leaf)
    return os.path.abspath(os.path.expanduser(value))


def config_dir() -> str:
    """Return the writable per-user Kilix configuration directory."""
    override = os.environ.get("KITTY_CONFIG_DIRECTORY")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return _owned_dir("KILIX_CONFIG_HOME", "config")


def cache_dir() -> str:
    return _owned_dir("KILIX_CACHE_HOME", "cache")


def data_dir() -> str:
    return _owned_dir("KILIX_DATA_HOME", "data")


def state_dir() -> str:
    return _owned_dir("KILIX_STATE_DIRECTORY", "state")


def session_dir() -> str:
    return _owned_dir("KILIX_SESSION_HOME", "session")


def telemetry_dir() -> str:
    value = os.environ.get("KILIX_TELEMETRY_RUNTIME") or os.path.join(
        session_dir(), "telemetry")
    return os.path.abspath(os.path.expanduser(value))


def build_dir() -> str:
    return _owned_dir("KILIX_BUILD_DIRECTORY", "build")


def defaults_dir() -> str:
    """Return the read-only tracked configuration/default-assets directory."""
    return os.path.join(kilix_home(), "config")


def launcher() -> str:
    """Return the Kilix launcher path."""
    return os.path.join(kilix_home(), "kilix")


def kitten_candidates() -> tuple[str, str]:
    """Return known kitten launcher candidates for the host engine."""
    return (
        os.path.join(build_dir(), "current", "src", "kitty", "launcher", "kitten"),
        os.path.join(_owned_dir("KILIX_PREBUILT_HOME", "prebuilt/kitty.app"),
                     "bin", "kitten"),
    )


__all__ = [
    "build_dir",
    "cache_dir",
    "config_dir",
    "data_dir",
    "defaults_dir",
    "kilix_home",
    "kitten_candidates",
    "launcher",
    "kilix95_home",
    "session_dir",
    "source_home",
    "state_dir",
    "storage_home",
    "telemetry_dir",
]
