"""Validated content-catalog model."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PREFERRED_SIZE = re.compile(r"^[1-9][0-9]{0,4}x[1-9][0-9]{0,4}$")
_HEX = frozenset("0123456789abcdef")
_SOURCE_TYPES = frozenset(("git", "archive", "system", "custom"))
_INSTALLABLE_SOURCE_TYPES = frozenset(("git", "archive"))
_LAUNCH_MODES = frozenset(("terminal", "run", "xpane", "browse", "window", "custom"))
_SCHEMA_VERSIONS = frozenset((1, 2, 3))
_ROOT_KEYS = frozenset(("schema_version", "packages", "content"))
_PACKAGE_KEYS = frozenset(("id", "source", "build", "dependency_hint"))
_ENTRY_KEYS = frozenset(
    (
        "id",
        "label",
        "kind",
        "icon",
        "description",
        "source",
        "package",
        "binary",
        "command",
        "build",
        "dependency_hint",
        "capabilities",
        "actions",
        "accepts",
        "lifecycle",
        "launch",
    )
)
_LAUNCH_KEYS = frozenset(("mode", "preferred_size"))
_ACTION_KEYS = frozenset(("argv", "accepts_input", "description"))
_LIFECYCLE_KEYS = frozenset(
    (
        "single_instance",
        "requires_kilix_session",
        "degrades_inplace",
        "preserve_on_failure",
        "startup_timeout_seconds",
    )
)
_SCHEMA_THREE_ENTRY_KEYS = frozenset(
    ("command", "actions", "accepts", "lifecycle")
)
_SOURCE_KEYS = {
    "git": frozenset(("type", "repository", "ref")),
    "archive": frozenset(("type", "urls", "sha256")),
    "system": frozenset(("type",)),
    "custom": frozenset(("type",)),
}
_MAX_CATALOG_BYTES = 1024 * 1024
_MAX_CONTENT_ENTRIES = 4096
_MAX_ACTIONS = 64
_MAX_ACCEPTED_INPUTS = 64


class CatalogError(ValueError):
    """The catalog is structurally invalid or violates its trust contract."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CatalogError(f"{label} must be an object")
    return value


def _known_keys(raw: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = tuple(key for key in raw if key not in allowed)
    if unknown:
        names = ", ".join(sorted(repr(key) for key in unknown))
        raise CatalogError(f"{label} has unknown field(s): {names}")


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError(f"catalog JSON contains duplicate field {key!r}")
        result[key] = value
    return result


def _valid_text(value: str) -> bool:
    if "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _exact_hex(value: str, length: int, label: str) -> str:
    if len(value) != length or not set(value) <= _HEX:
        raise CatalogError(
            f"{label} must be exactly {length} lowercase hexadecimal characters"
        )
    return value


def _relative_path(value: str, label: str) -> str:
    if not value or not _valid_text(value) or os.path.isabs(value):
        raise CatalogError(f"{label} must be a non-empty relative path")
    normalized = os.path.normpath(value)
    if normalized in (".", "..") or normalized.startswith("../"):
        raise CatalogError(f"{label} escapes its content directory")
    return normalized


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item or not _valid_text(item) for item in value
    ):
        raise CatalogError(f"{label} must be an array of non-empty strings")
    return tuple(value)


def _content_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise CatalogError(f"invalid {label}: {value!r}")
    return value


def _source_fields(
    value: Any, label: str
) -> tuple[str, str, str, tuple[str, ...], str]:
    source = _mapping(value, label)
    source_type = source.get("type", "")
    if not isinstance(source_type, str):
        raise CatalogError(f"{label} type must be a string")
    if source_type not in _SOURCE_TYPES:
        raise CatalogError(f"{label} has unsupported type {source_type!r}")
    _known_keys(source, _SOURCE_KEYS[source_type], label)

    repository = source.get("repository", "")
    ref = source.get("ref", "")
    urls = _string_tuple(source.get("urls"), f"{label}.urls")
    sha256 = source.get("sha256", "")
    if source_type == "git":
        if (
            not isinstance(repository, str)
            or not repository
            or not _valid_text(repository)
        ):
            raise CatalogError(f"{label} git repository is required")
        if not isinstance(ref, str):
            raise CatalogError(f"{label} git ref must be a string")
        _exact_hex(ref, 40, f"{label}.ref")
    elif source_type == "archive":
        if not urls:
            raise CatalogError(f"{label} archive URLs are required")
        if not isinstance(sha256, str):
            raise CatalogError(f"{label} archive sha256 must be a string")
        _exact_hex(sha256, 64, f"{label}.sha256")
    return source_type, repository, ref, urls, sha256


@dataclass(frozen=True)
class ActionSpec:
    """One named, argv-only application action."""

    action_id: str
    argv: tuple[str, ...] = ()
    accepts_input: bool = False
    description: str = ""

    @classmethod
    def from_mapping(cls, action_id: str, raw: Mapping[str, Any]) -> ActionSpec:
        action_id = _content_id(action_id, "action id")
        raw = _mapping(raw, f"action {action_id!r}")
        _known_keys(raw, _ACTION_KEYS, f"action {action_id!r}")
        accepts_input = raw.get("accepts_input", False)
        if type(accepts_input) is not bool:
            raise CatalogError(
                f"action {action_id!r}.accepts_input must be a boolean"
            )
        description = raw.get("description", "")
        if not isinstance(description, str) or not _valid_text(description):
            raise CatalogError(
                f"action {action_id!r}.description must be a string"
            )
        return cls(
            action_id=action_id,
            argv=_string_tuple(raw.get("argv"), f"action {action_id!r}.argv"),
            accepts_input=accepts_input,
            description=description,
        )


@dataclass(frozen=True)
class LifecycleSpec:
    """Host-facing application lifetime and fallback policy."""

    single_instance: bool = False
    requires_kilix_session: bool = False
    degrades_inplace: bool = True
    preserve_on_failure: bool = True
    startup_timeout_seconds: int = 0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> LifecycleSpec:
        if raw is None:
            return cls()
        raw = _mapping(raw, "lifecycle")
        _known_keys(raw, _LIFECYCLE_KEYS, "lifecycle")
        values: dict[str, bool] = {}
        defaults = {
            "single_instance": False,
            "requires_kilix_session": False,
            "degrades_inplace": True,
            "preserve_on_failure": True,
        }
        for key, default in defaults.items():
            value = raw.get(key, default)
            if type(value) is not bool:
                raise CatalogError(f"lifecycle.{key} must be a boolean")
            values[key] = value
        timeout = raw.get("startup_timeout_seconds", 0)
        if type(timeout) is not int or not 0 <= timeout <= 3600:
            raise CatalogError(
                "lifecycle.startup_timeout_seconds must be an integer from 0 to 3600"
            )
        return cls(**values, startup_timeout_seconds=timeout)


def _action_specs(value: Any, label: str) -> tuple[ActionSpec, ...]:
    if value is None:
        return ()
    actions = _mapping(value, label)
    if len(actions) > _MAX_ACTIONS:
        raise CatalogError(f"{label} has more than {_MAX_ACTIONS} actions")
    return tuple(
        ActionSpec.from_mapping(action_id, raw)
        for action_id, raw in actions.items()
    )


@dataclass(frozen=True)
class PackageSpec:
    """One immutable installation that may provide several content entries."""

    package_id: str
    source_type: str
    repository: str = ""
    ref: str = ""
    urls: tuple[str, ...] = ()
    sha256: str = ""
    build: tuple[str, ...] = ()
    dependency_hint: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> PackageSpec:
        raw = _mapping(raw, "package entry")
        _known_keys(raw, _PACKAGE_KEYS, "package entry")
        try:
            package_id = _content_id(raw["id"], "package id")
            source = raw["source"]
        except KeyError as exc:
            raise CatalogError(f"package entry is missing {exc.args[0]!r}") from exc
        source_type, repository, ref, urls, sha256 = _source_fields(
            source, f"{package_id}.source"
        )
        if source_type not in _INSTALLABLE_SOURCE_TYPES:
            raise CatalogError(
                f"{package_id}: package source must be installable, got "
                f"{source_type!r}"
            )
        dependency_hint = raw.get("dependency_hint", "")
        if not isinstance(dependency_hint, str) or not _valid_text(dependency_hint):
            raise CatalogError(f"{package_id}: dependency_hint must be a string")
        return cls(
            package_id=package_id,
            source_type=source_type,
            repository=repository,
            ref=ref,
            urls=urls,
            sha256=sha256,
            build=_string_tuple(raw.get("build"), f"{package_id}.build"),
            dependency_hint=dependency_hint,
        )

    def supplies(self, spec: ContentSpec) -> bool:
        """Whether a flattened content specification still matches this package."""
        return (
            spec.package_id == self.package_id
            and spec.source_type == self.source_type
            and spec.repository == self.repository
            and spec.ref == self.ref
            and spec.urls == self.urls
            and spec.sha256 == self.sha256
            and spec.build == self.build
            and spec.dependency_hint == self.dependency_hint
        )


@dataclass(frozen=True)
class ContentSpec:
    content_id: str
    label: str
    kind: str
    icon: str
    description: str
    source_type: str
    repository: str = ""
    ref: str = ""
    urls: tuple[str, ...] = ()
    sha256: str = ""
    binary: str = ""
    command: tuple[str, ...] = ()
    build: tuple[str, ...] = ()
    dependency_hint: str = ""
    capabilities: tuple[str, ...] = ()
    actions: tuple[ActionSpec, ...] = ()
    accepts: tuple[str, ...] = ()
    lifecycle: LifecycleSpec = LifecycleSpec()
    launch_mode: str = "terminal"
    preferred_size: str = ""
    package_id: str = ""

    @property
    def install_id(self) -> str:
        """Directory/cache identity; shared by entries from the same package."""
        return self.package_id or self.content_id

    def get_action(self, action_id: str) -> ActionSpec | None:
        return next(
            (action for action in self.actions if action.action_id == action_id),
            None,
        )

    def require_action(self, action_id: str) -> ActionSpec:
        action = self.get_action(action_id)
        if action is None:
            raise CatalogError(
                f"{self.content_id}: unknown application action {action_id!r}"
            )
        return action

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        packages: Mapping[str, PackageSpec] | None = None,
    ) -> ContentSpec:
        raw = _mapping(raw, "content entry")
        _known_keys(raw, _ENTRY_KEYS, "content entry")
        try:
            content_id = _content_id(raw["id"], "content id")
            label = raw["label"]
        except KeyError as exc:
            raise CatalogError(f"content entry is missing {exc.args[0]!r}") from exc
        if not isinstance(label, str) or not label.strip() or not _valid_text(label):
            raise CatalogError(f"{content_id}: label must be a non-empty string")

        package_id = raw.get("package", "")
        if not isinstance(package_id, str):
            raise CatalogError(f"{content_id}: package must be a string")
        if package_id:
            _content_id(package_id, "package id")
            if "source" in raw:
                raise CatalogError(f"{content_id}: source and package are mutually exclusive")
            overridden = tuple(
                field for field in ("build", "dependency_hint") if field in raw
            )
            if overridden:
                raise CatalogError(
                    f"{content_id}: package-owned field(s) cannot be overridden: "
                    + ", ".join(overridden)
                )
            package = (packages or {}).get(package_id)
            if package is None:
                raise CatalogError(f"{content_id}: unknown package {package_id!r}")
            source_type = package.source_type
            repository = package.repository
            ref = package.ref
            urls = package.urls
            sha256 = package.sha256
            build = package.build
            dependency_hint = package.dependency_hint
        else:
            try:
                source = raw["source"]
            except KeyError as exc:
                raise CatalogError(
                    f"{content_id}: either source or package is required"
                ) from exc
            source_type, repository, ref, urls, sha256 = _source_fields(
                source, f"{content_id}.source"
            )
            build = _string_tuple(raw.get("build"), f"{content_id}.build")
            dependency_hint = raw.get("dependency_hint", "")
            if not isinstance(dependency_hint, str) or not _valid_text(
                dependency_hint
            ):
                raise CatalogError(f"{content_id}: dependency_hint must be a string")

        binary = raw.get("binary", "")
        if not isinstance(binary, str):
            raise CatalogError(f"{content_id}: binary must be a string")
        command = _string_tuple(raw.get("command"), f"{content_id}.command")
        if "command" in raw and not command:
            raise CatalogError(f"{content_id}: command must not be empty")
        launch = raw.get("launch", {})
        launch = _mapping(launch, f"{content_id}.launch")
        _known_keys(launch, _LAUNCH_KEYS, f"{content_id}.launch")
        launch_mode = launch.get("mode", "terminal")
        if not isinstance(launch_mode, str):
            raise CatalogError(f"{content_id}: launch mode must be a string")
        if launch_mode not in _LAUNCH_MODES:
            raise CatalogError(f"{content_id}: unsupported launch mode {launch_mode!r}")

        if binary:
            binary = _relative_path(binary, f"{content_id}.binary")
        if source_type in ("git", "archive") and not binary:
            raise CatalogError(
                f"{content_id}: installable content requires a binary path"
            )
        if source_type in ("git", "archive") and command:
            raise CatalogError(
                f"{content_id}: installable content cannot declare a system command"
            )
        if binary and command:
            raise CatalogError(
                f"{content_id}: binary and command are mutually exclusive"
            )

        actions = _action_specs(raw.get("actions"), f"{content_id}.actions")
        accepts = _string_tuple(raw.get("accepts"), f"{content_id}.accepts")
        if len(accepts) > _MAX_ACCEPTED_INPUTS:
            raise CatalogError(
                f"{content_id}.accepts has more than {_MAX_ACCEPTED_INPUTS} inputs"
            )
        lifecycle = LifecycleSpec.from_mapping(raw.get("lifecycle"))

        strings = {}
        for key in ("kind", "icon", "description"):
            value = raw.get(key, "")
            if not isinstance(value, str) or not _valid_text(value):
                raise CatalogError(f"{content_id}: {key} must be a string")
            strings[key] = value
        preferred_size = launch.get("preferred_size", "")
        if not isinstance(preferred_size, str):
            raise CatalogError(f"{content_id}: preferred_size must be a string")
        if preferred_size and not _PREFERRED_SIZE.fullmatch(preferred_size):
            raise CatalogError(
                f"{content_id}: preferred_size must use positive WIDTHxHEIGHT pixels"
            )

        return cls(
            content_id=content_id,
            label=label.strip(),
            kind=strings["kind"] or "game",
            icon=strings["icon"],
            description=strings["description"],
            source_type=source_type,
            repository=repository,
            ref=ref,
            urls=urls,
            sha256=sha256,
            binary=binary,
            command=command,
            build=build,
            dependency_hint=dependency_hint,
            capabilities=_string_tuple(
                raw.get("capabilities"), f"{content_id}.capabilities"
            ),
            actions=actions,
            accepts=accepts,
            lifecycle=lifecycle,
            launch_mode=launch_mode,
            preferred_size=preferred_size,
            package_id=package_id,
        )


class Catalog:
    """An immutable, uniquely keyed content catalog."""

    def __init__(
        self,
        entries: Iterable[ContentSpec],
        schema_version: int = 1,
        *,
        packages: Iterable[PackageSpec] = (),
    ):
        if type(schema_version) is not int or schema_version not in _SCHEMA_VERSIONS:
            raise CatalogError(
                f"unsupported catalog schema version: {schema_version!r}"
            )
        by_package: dict[str, PackageSpec] = {}
        for package in packages:
            if not isinstance(package, PackageSpec):
                raise CatalogError("catalog packages must be PackageSpec instances")
            if package.package_id in by_package:
                raise CatalogError(f"duplicate package id: {package.package_id}")
            by_package[package.package_id] = package
        if schema_version == 1 and by_package:
            raise CatalogError("catalog schema version 1 cannot define packages")

        by_id: dict[str, ContentSpec] = {}
        provided: dict[str, list[ContentSpec]] = {}
        used_packages: set[str] = set()
        for entry in entries:
            if not isinstance(entry, ContentSpec):
                raise CatalogError("catalog entries must be ContentSpec instances")
            if schema_version < 3 and (
                entry.command
                or entry.actions
                or entry.accepts
                or entry.lifecycle != LifecycleSpec()
            ):
                raise CatalogError(
                    f"{entry.content_id}: application metadata requires schema version 3"
                )
            if entry.content_id in by_id:
                raise CatalogError(f"duplicate content id: {entry.content_id}")
            if (
                entry.content_id in by_package
                and entry.package_id != entry.content_id
            ):
                raise CatalogError(
                    f"{entry.content_id}: content id conflicts with package install "
                    "identity"
                )
            if entry.package_id:
                package = by_package.get(entry.package_id)
                if package is None:
                    raise CatalogError(
                        f"{entry.content_id}: unknown package {entry.package_id!r}"
                    )
                if not package.supplies(entry):
                    raise CatalogError(
                        f"{entry.content_id}: flattened package metadata does not match "
                        f"{entry.package_id!r}"
                    )
                used_packages.add(entry.package_id)
            by_id[entry.content_id] = entry
            provided.setdefault(entry.install_id, []).append(entry)
        unused = tuple(
            package_id for package_id in by_package if package_id not in used_packages
        )
        if unused:
            raise CatalogError(
                "unused package(s): " + ", ".join(sorted(repr(item) for item in unused))
            )
        self.schema_version = schema_version
        self._entries = tuple(by_id.values())
        self._by_id = MappingProxyType(by_id)
        self._packages = tuple(by_package.values())
        self._by_package = MappingProxyType(by_package)
        self._provided = MappingProxyType(
            {package_id: tuple(items) for package_id, items in provided.items()}
        )

    @property
    def packages(self) -> tuple[PackageSpec, ...]:
        return self._packages

    def __iter__(self) -> Iterator[ContentSpec]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, content_id: str) -> ContentSpec | None:
        return self._by_id.get(content_id)

    def require(self, content_id: str) -> ContentSpec:
        try:
            return self._by_id[content_id]
        except KeyError as exc:
            raise CatalogError(f"unknown content id: {content_id}") from exc

    def get_package(self, package_id: str) -> PackageSpec | None:
        return self._by_package.get(package_id)

    def require_package(self, package_id: str) -> PackageSpec:
        try:
            return self._by_package[package_id]
        except KeyError as exc:
            raise CatalogError(f"unknown package id: {package_id}") from exc

    def provided_by(self, install_id: str) -> tuple[ContentSpec, ...]:
        """Every entry sharing one installation/cache identity."""
        return self._provided.get(install_id, ())

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> Catalog:
        raw = _mapping(raw, "catalog root")
        _known_keys(raw, _ROOT_KEYS, "catalog root")
        entries = raw.get("content")
        if not isinstance(entries, list):
            raise CatalogError("catalog content must be an array")
        if len(entries) > _MAX_CONTENT_ENTRIES:
            raise CatalogError(
                f"catalog has more than {_MAX_CONTENT_ENTRIES} content entries"
            )
        version = raw.get("schema_version")
        if type(version) is not int:
            raise CatalogError("catalog schema_version must be an integer")
        if version not in _SCHEMA_VERSIONS:
            raise CatalogError(f"unsupported catalog schema version: {version!r}")
        if version < 3:
            for item in entries:
                if isinstance(item, Mapping):
                    newer = tuple(
                        key for key in _SCHEMA_THREE_ENTRY_KEYS if key in item
                    )
                    if newer:
                        raise CatalogError(
                            "application metadata field(s) require schema version 3: "
                            + ", ".join(sorted(newer))
                        )
        raw_packages = raw.get("packages", [])
        if not isinstance(raw_packages, list):
            raise CatalogError("catalog packages must be an array")
        if len(raw_packages) > _MAX_CONTENT_ENTRIES:
            raise CatalogError(
                f"catalog has more than {_MAX_CONTENT_ENTRIES} package entries"
            )
        if version == 1 and raw_packages:
            raise CatalogError("catalog schema version 1 cannot define packages")
        packages = tuple(PackageSpec.from_mapping(item) for item in raw_packages)
        by_package: dict[str, PackageSpec] = {}
        for package in packages:
            if package.package_id in by_package:
                raise CatalogError(f"duplicate package id: {package.package_id}")
            by_package[package.package_id] = package
        parsed = tuple(
            ContentSpec.from_mapping(item, packages=by_package) for item in entries
        )
        return cls(parsed, version, packages=packages)

    @classmethod
    def loads(cls, payload: str, *, label: str = "catalog") -> Catalog:
        if not isinstance(payload, str):
            raise CatalogError(f"{label} JSON must be text")
        try:
            payload_size = (
                len(payload) if payload.isascii() else len(payload.encode("utf-8"))
            )
        except UnicodeError as exc:
            raise CatalogError(f"could not parse {label}: {exc}") from exc
        if payload_size > _MAX_CATALOG_BYTES:
            raise CatalogError(f"{label} exceeds the 1 MiB size limit")
        try:
            raw = json.loads(payload, object_pairs_hook=_json_object)
        except CatalogError:
            raise
        except (json.JSONDecodeError, RecursionError) as exc:
            raise CatalogError(f"could not parse {label}: {exc}") from exc
        return cls.from_mapping(raw)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> Catalog:
        try:
            with open(path, "rb") as stream:
                payload_bytes = stream.read(_MAX_CATALOG_BYTES + 1)
            if len(payload_bytes) > _MAX_CATALOG_BYTES:
                raise CatalogError(f"catalog {path} exceeds the 1 MiB size limit")
            payload = payload_bytes.decode("utf-8")
        except CatalogError:
            raise
        except (OSError, UnicodeError) as exc:
            raise CatalogError(f"could not load catalog {path}: {exc}") from exc
        return cls.loads(payload, label=f"catalog {path}")
