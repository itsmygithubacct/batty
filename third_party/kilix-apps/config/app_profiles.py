"""Private per-run browser profiles used by Kilix GUI containers.

Chromium and Firefox otherwise reuse a process attached to their default
profile.  That process can live on the host display, so a nominally contained
launch may hand its URL to an outside window.  These helpers give each
container a short-lived profile while respecting any profile the caller chose
explicitly.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import time


CHROMIUM_COMMANDS = {
    "brave-browser", "chromium", "chromium-browser", "google-chrome",
    "google-chrome-stable", "microsoft-edge", "microsoft-edge-stable",
    "opera", "vivaldi", "vivaldi-stable",
}
FIREFOX_COMMANDS = {"firefox", "firefox-esr"}
APP_PROFILE_STALE_SECONDS = 7 * 24 * 60 * 60
APP_PROFILE_NAME = re.compile(
    r"^.+?-(?P<pid>\d+)(?:-(?P<start>\d+))?-[A-Za-z0-9_]+$")


def _require_private_directory(path: str, label: str) -> None:
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid()):
        raise RuntimeError(f"unsafe {label} directory: {path}")
    os.chmod(path, 0o700)


def _process_start(pid: int) -> int | None:
    try:
        # /proc/PID/stat starts at field 3 after the final ') '; starttime is
        # field 22. Splitting there remains correct when the command has spaces.
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            fields = handle.read().rsplit(") ", 1)[1].split()
        return int(fields[19])
    except (IndexError, OSError, OverflowError, ValueError):
        return None


def _pid_is_running(pid: int, expected_start: int | None = None) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError, ValueError):
        return False
    except PermissionError:
        return True
    if expected_start is not None:
        return _process_start(pid) == expected_start
    return True


def cleanup_app_profile(profile: str | None) -> None:
    """Remove one profile only when it remains a user-owned real directory."""
    if not profile:
        return
    try:
        info = os.lstat(profile)
        parent = os.lstat(os.path.dirname(profile))
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.geteuid()
                or not stat.S_ISDIR(parent.st_mode)
                or stat.S_ISLNK(parent.st_mode)
                or parent.st_uid != os.geteuid()):
            return
        shutil.rmtree(profile)
    except (FileNotFoundError, OSError):
        pass


def cleanup_stale_app_profiles(parent: str, now: float | None = None) -> None:
    """Collect old abandoned profiles without touching live container PIDs."""
    now = time.time() if now is None else now
    try:
        entries = list(os.scandir(parent))
    except OSError:
        return
    for entry in entries:
        match = APP_PROFILE_NAME.fullmatch(entry.name)
        if not match:
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.geteuid()
                or now - info.st_mtime < APP_PROFILE_STALE_SECONDS
                or _pid_is_running(
                    int(match.group("pid")),
                    int(match.group("start"))
                    if match.group("start") is not None else None)):
            continue
        cleanup_app_profile(entry.path)


def _private_app_profile_parent() -> str:
    session = os.environ.get("KILIX_SESSION_HOME")
    if not session:
        storage = os.environ.get(
            "KILIX_STORAGE_HOME",
            os.path.expanduser("~/.local/gpu_terminal/kilix"))
        session = os.path.join(storage, "session")
    session = os.path.abspath(os.path.expanduser(session))
    os.makedirs(session, mode=0o700, exist_ok=True)
    _require_private_directory(session, "GUI session")
    parent = os.path.join(session, "app-profiles")
    os.makedirs(parent, mode=0o700, exist_ok=True)
    _require_private_directory(parent, "GUI profile")
    cleanup_stale_app_profiles(parent)
    return parent


def _new_profile(name: str) -> str:
    start = _process_start(os.getpid())
    owner = f"{os.getpid()}-{start}" if start is not None else str(os.getpid())
    profile = tempfile.mkdtemp(
        prefix=f"{name}-{owner}-", dir=_private_app_profile_parent())
    os.chmod(profile, 0o700)
    return profile


def prepare_app_command(command: list[str]) -> tuple[list[str], str | None]:
    """Return a singleton-safe browser argv and its temporary profile path."""
    command = list(command)
    if not command:
        return command, None
    name = os.path.basename(command[0]).lower()
    if name in CHROMIUM_COMMANDS:
        if any(arg == "--user-data-dir"
               or arg.startswith("--user-data-dir=")
               for arg in command[1:]):
            return command, None
        profile = _new_profile(name)
        return [
            command[0], f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", *command[1:],
        ], profile
    if name in FIREFOX_COMMANDS:
        # Firefox accepts both one- and two-dash spellings, and its documented
        # ProfileManager forms use capitals. Normalize before comparison.
        args_lower = [arg.lower() for arg in command[1:]]
        profile_flags = {
            "-p", "-profile", "--profile", "-profilemanager",
            "--profilemanager",
        }
        if any(arg in profile_flags
               or arg.startswith(("-profile=", "--profile="))
               for arg in args_lower):
            return command, None
        profile = _new_profile(name)
        no_remote = any(
            arg in {"-no-remote", "--no-remote"} for arg in args_lower)
        return [command[0], "--profile", profile,
                *([] if no_remote else ["--no-remote"]),
                *command[1:]], profile
    return command, None


__all__ = [
    "APP_PROFILE_STALE_SECONDS",
    "cleanup_app_profile",
    "cleanup_stale_app_profiles",
    "prepare_app_command",
]
