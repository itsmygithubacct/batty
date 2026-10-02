"""Canonical writable storage for Kilix's bundled desktop fallback."""

import os
import tempfile


def storage_home():
    base = os.environ.get("GPU_TERMINAL_HOME") or os.path.expanduser(
        "~/.local/gpu_terminal")
    value = os.environ.get("KILIX_STORAGE_HOME") or os.path.join(base, "kilix")
    return os.path.abspath(os.path.expanduser(value))


def _owned(env_name, leaf):
    value = os.environ.get(env_name) or os.path.join(storage_home(), leaf)
    return os.path.abspath(os.path.expanduser(value))


def config_dir(*parts):
    return os.path.join(_owned("KILIX_CONFIG_HOME", "config"), *parts)


def state_dir(*parts):
    return os.path.join(_owned("KILIX_STATE_DIRECTORY", "state"), *parts)


def cache_dir(*parts):
    return os.path.join(_owned("KILIX_CACHE_HOME", "cache"), *parts)


def data_dir(*parts):
    return os.path.join(_owned("KILIX_DATA_HOME", "data"), *parts)


def session_dir(*parts):
    return os.path.join(_owned("KILIX_SESSION_HOME", "session"), *parts)


def ensure_private_dir(path):
    """Create a Kilix-owned directory and keep its final component private."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def atomic_write_private(path, writer):
    """Atomically replace a private file without following the destination."""
    directory = ensure_private_dir(os.path.dirname(path))
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            writer(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
