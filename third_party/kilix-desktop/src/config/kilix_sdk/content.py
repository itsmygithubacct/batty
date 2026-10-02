"""Pinned application/game content exposed by the Kilix host SDK."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ._packages import load_pinned_package


def _load_shared_package():
    root = Path(__file__).resolve().parents[2]
    return load_pinned_package(
        "kilix_content",
        (
            root / "third_party" / "kilix-content" / "src",
            root.parent / "kilix-modules" / "kilix-content" / "src",
        ),
        "kilix-content is unavailable; initialize Kilix submodules with: "
        "git submodule update --init --recursive",
    )


_shared = _load_shared_package()

Catalog = _shared.Catalog
CatalogError = _shared.CatalogError
ContentSpec = _shared.ContentSpec
PackageSpec = _shared.PackageSpec
ActionSpec = _shared.ActionSpec
LifecycleSpec = _shared.LifecycleSpec
InstallError = _shared.InstallError
Installer = _shared.Installer
default_catalog = _shared.default_catalog
download = _shared.download
safe_extract_tar = _shared.safe_extract_tar
safe_extract_zip = _shared.safe_extract_zip
sha256_file = _shared.sha256_file
verify_git_checkout = _shared.verify_git_checkout

_APPLICATION_SURFACES = frozenset(("current", "pane", "window"))
_CUSTOM_APPLICATION_VERBS = {
    # ``custom`` intentionally carries no executable metadata. DOSBox is the
    # one host-owned custom application in the catalog; desktop/games.py owns
    # its verified payload and display-aware launcher.
    "dosbox": ("games", "play"),
}


@dataclass(frozen=True)
class ApplicationPlan:
    """A catalog application's host command and presentation metadata."""

    content_id: str
    label: str
    icon: str
    surface: str
    launch_mode: str
    argv: tuple[str, ...]
    preferred_size: tuple[int, int] | None
    action: str
    capabilities: tuple[str, ...]
    accepts: tuple[str, ...]
    single_instance: bool
    requires_kilix_session: bool
    degrades_inplace: bool
    preserve_on_failure: bool
    startup_timeout_seconds: int


def _preferred_size(value: str) -> tuple[int, int] | None:
    if not value:
        return None
    width, height = value.split("x", 1)
    return int(width), int(height)


def application_plan(
    content_id: str,
    surface: str = "current",
    arguments: Iterable[str] = (),
    *,
    catalog=None,
    launcher: str | None = None,
    action: str | None = None,
) -> ApplicationPlan:
    """Plan one application for an in-place, pane, or desktop-window host.

    Pane creation belongs to the desktop because Kilix 95 uses XPane while
    Cap, Land, and the TUI use terminal tabs. Once that surface exists, every
    provider executes the same catalog-aware host verb returned here.
    """
    if surface not in _APPLICATION_SURFACES:
        raise ValueError(f"unsupported application surface: {surface!r}")
    if catalog is None:
        catalog = default_catalog()
    spec = catalog.require(content_id)
    if spec.kind != "app":
        raise CatalogError(
            f"{content_id} is {spec.kind!r} content, not an application"
        )
    if isinstance(arguments, (str, bytes)):
        raise TypeError("application arguments must be an iterable of arguments")
    forwarded = tuple(str(argument) for argument in arguments)
    host = launcher or str(Path(__file__).resolve().parents[2] / "kilix")
    selected_action = None
    action_arguments: tuple[str, ...] = ()
    if action is not None:
        selected_action = spec.require_action(action)
        if len(forwarded) > int(selected_action.accepts_input):
            expected = (
                "at most one input" if selected_action.accepts_input else "no input"
            )
            raise ValueError(f"{content_id} action {action!r} accepts {expected}")
        action_arguments = (*selected_action.argv, *forwarded)
    else:
        action_arguments = forwarded

    if spec.source_type in ("git", "archive"):
        launch_action = (
            "window"
            if surface == "window" and spec.launch_mode == "terminal"
            else "run"
        )
        argv = (host, "app", launch_action, spec.content_id)
        if selected_action is not None:
            argv += ("--action", selected_action.action_id)
            if forwarded:
                argv += ("--", *forwarded)
        elif forwarded:
            argv += ("--", *forwarded)
    elif spec.source_type == "system":
        if surface == "window" and spec.launch_mode == "terminal":
            argv = (host, "app", "window", spec.content_id)
            if selected_action is not None:
                argv += ("--action", selected_action.action_id)
                if forwarded:
                    argv += ("--", *forwarded)
            elif forwarded:
                argv += ("--", *forwarded)
        else:
            command = tuple(getattr(spec, "command", ()))
            if not command:
                command = (spec.binary or spec.content_id,)
            if command[0] == "kilix":
                command = (host, *command[1:])
            argv = (*command, *action_arguments)
    elif spec.source_type == "custom" and spec.content_id in _CUSTOM_APPLICATION_VERBS:
        argv = (
            host,
            *_CUSTOM_APPLICATION_VERBS[spec.content_id],
            spec.content_id,
            *action_arguments,
        )
    else:
        raise CatalogError(
            f"{content_id} has no shared launch contract for {spec.source_type!r}"
        )
    lifecycle = getattr(spec, "lifecycle", LifecycleSpec())
    return ApplicationPlan(
        content_id=spec.content_id,
        label=spec.label or spec.content_id,
        icon=spec.icon or "app",
        surface=surface,
        launch_mode=spec.launch_mode,
        argv=argv,
        preferred_size=_preferred_size(spec.preferred_size),
        action=selected_action.action_id if selected_action is not None else "",
        capabilities=tuple(spec.capabilities),
        accepts=tuple(getattr(spec, "accepts", ())),
        single_instance=lifecycle.single_instance,
        requires_kilix_session=lifecycle.requires_kilix_session,
        degrades_inplace=lifecycle.degrades_inplace,
        preserve_on_failure=lifecycle.preserve_on_failure,
        startup_timeout_seconds=lifecycle.startup_timeout_seconds,
    )


def entries(catalog=None):
    """Every catalog record, ordered by label.

    ``Catalog`` is already iterable, but nothing offered desktops a settled
    order or a safe way to ask for the default catalog, so each one grew its
    own loop -- and Kilix 95 grew a hand-written ID table instead. A desktop
    that has to name its content by hand silently omits anything added later,
    which is exactly the drift this exists to remove.

    Passing ``catalog`` is for tests and for a desktop that has loaded a
    specific catalog file; the default resolves the host's own.
    """
    if catalog is None:
        catalog = default_catalog()
    return sorted(catalog, key=lambda spec: (spec.label or spec.content_id).casefold())


def grouped(catalog=None):
    """Catalog records bucketed by ``kind``, each bucket ordered by label.

    Returns a plain ``dict`` of ``{kind: [ContentSpec, ...]}``. Kinds are
    whatever the catalog declares rather than a fixed set here, so a new kind
    upstream reaches menus without a matching change in every desktop.
    """
    buckets: dict[str, list] = {}
    for spec in entries(catalog):
        buckets.setdefault(spec.kind or "app", []).append(spec)
    return buckets


def _lifecycle_record(spec) -> dict[str, object]:
    lifecycle = getattr(spec, "lifecycle", LifecycleSpec())
    return {
        "single_instance": getattr(lifecycle, "single_instance", False),
        "requires_kilix_session": getattr(
            lifecycle, "requires_kilix_session", False),
        "degrades_inplace": getattr(lifecycle, "degrades_inplace", True),
        "preserve_on_failure": getattr(
            lifecycle, "preserve_on_failure", True),
        "startup_timeout_seconds": getattr(
            lifecycle, "startup_timeout_seconds", 0),
    }


def menu_records(catalog=None):
    """Catalog records as plain dicts, for desktops that render text menus.

    Deliberately not ``ContentSpec`` objects: a menu generator should be
    testable without importing the content module, and it has no business
    reaching the install machinery hanging off a spec.
    """
    return [
        {
            "id": spec.content_id,
            "label": spec.label or spec.content_id,
            "kind": spec.kind or "app",
            "icon": spec.icon or "",
            "description": spec.description or "",
            "source_type": getattr(spec, "source_type", ""),
            "package_id": getattr(spec, "package_id", ""),
            "install_id": getattr(spec, "install_id", spec.content_id),
            "binary": getattr(spec, "binary", ""),
            "command": list(getattr(spec, "command", ())),
            "launch_mode": getattr(spec, "launch_mode", "terminal"),
            "preferred_size": getattr(spec, "preferred_size", ""),
            "capabilities": list(getattr(spec, "capabilities", ())),
            "actions": {
                item.action_id: {
                    "argv": list(item.argv),
                    "accepts_input": item.accepts_input,
                    "description": item.description,
                }
                for item in getattr(spec, "actions", ())
            },
            "accepts": list(getattr(spec, "accepts", ())),
            "lifecycle": _lifecycle_record(spec),
        }
        for spec in entries(catalog)
    ]

__all__ = [
    "Catalog",
    "CatalogError",
    "ActionSpec",
    "ContentSpec",
    "LifecycleSpec",
    "PackageSpec",
    "ApplicationPlan",
    "InstallError",
    "Installer",
    "application_plan",
    "default_catalog",
    "download",
    "safe_extract_tar",
    "safe_extract_zip",
    "sha256_file",
    "verify_git_checkout",
]
