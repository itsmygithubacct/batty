"""Stable, versioned host API for external Kilix clients.

Kilix 95 and other hosted tools should import through this package instead of
reaching into implementation modules such as ``browse`` and ``gfx`` directly.
The first SDK layer is intentionally thin; it names the contract while the
underlying implementations continue to live in the existing host modules.
Providers may use :func:`require_compatible` during import so an unsupported
host fails with an actionable message instead of a later attribute error.
The compatibility promise follows semantic-version major/minor rules: SDK 1.x
keeps the 1.0 contract, while a provider may require a newer 1.y minor.
"""

import re


SDK_VERSION = "1.14.0"
SDK_API_VERSION = (1, 14)
__version__ = SDK_VERSION

_REQUIREMENT = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class IncompatibleSDKError(RuntimeError):
    """Raised when a provider requires an incompatible Kilix host SDK."""


def require_compatible(required: str = "1.0") -> None:
    """Require ``MAJOR.MINOR`` compatibility with this host SDK."""
    try:
        match = _REQUIREMENT.fullmatch(required)
    except TypeError as exc:
        raise IncompatibleSDKError(f"invalid Kilix SDK requirement: {required!r}") from exc
    if match is None:
        raise IncompatibleSDKError(f"invalid Kilix SDK requirement: {required!r}")
    try:
        wanted = int(match.group(1)), int(match.group(2))
    except ValueError as exc:
        raise IncompatibleSDKError(
            f"invalid Kilix SDK requirement: {required!r}") from exc
    have = SDK_API_VERSION
    if wanted[0] != have[0] or wanted > have:
        raise IncompatibleSDKError(
            f"desktop provider requires kilix_sdk {wanted[0]}.{wanted[1]}; "
            f"host provides {have[0]}.{have[1]}"
        )


__all__ = [
    "SDK_API_VERSION",
    "SDK_VERSION",
    "IncompatibleSDKError",
    "content",
    "graphics",
    "paths",
    "require_compatible",
    "settings",
    "state",
    "telemetry",
    "term",
    "tui_shell",
    "xapp",
    "xdgapps",
]
