"""Default-password reminder via the narrowly scoped Plebian-OS helper.

On systems without the helper every operation safely degrades to unavailable,
so the compatibility desktop remains usable outside Plebian-OS.
"""
import os
import shutil
import subprocess

HELPER = "/usr/local/sbin/plebian-os-passwd"


def available():
    return bool(shutil.which("sudo")) and os.access(HELPER, os.X_OK)


def is_default_password():
    """Return true only when the helper confirms the shipped password."""
    if not available():
        return False
    try:
        result = subprocess.run(
            ["sudo", "-n", HELPER, "check"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def change_password(new_password):
    """Ask the helper to replace the default password; return ``(ok, msg)``."""
    if not available():
        return False, "The password helper is not available on this system."
    try:
        result = subprocess.run(
            ["sudo", "-n", HELPER, "set"],
            input=new_password + "\n",
            text=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Could not run the password helper: {exc}"
    if result.returncode == 0:
        return True, "Your password has been changed."
    message = (result.stderr or result.stdout or "").strip()
    message = message.replace("plebian-os-passwd: ", "")
    return False, message or "The password could not be changed."
