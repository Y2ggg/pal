"""Configuration mounts and the read-only creation context resolver.

This module implements the second implementation gate only.  A configuration
mount is an PAL-owned record which snapshots the development context required
by P1; it is deliberately separate from production mounts and never writes to
the library's development or production trees.

Traceability: PRD-MOUNT-001, PRD-MOUNT-002, PRD-P0-004, PRD-TECH-001;
ACC-002, ACC-012.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .errors import IntegrityError, PathSafetyError
from .io import atomic_replace_json, fsync_directory, sha256_file, write_new_json
from .library import (
    _verify_reference,
    doctor_development_context,
    doctor_library,
    load_json_object,
    utc_now,
)
from .paths import canonical_existing_root, require_inside, require_safe_id
from .platform_support import is_link, validate_windows_path
from .schema_catalog import TARGET_CLIS, validate_config_instance, validate_instance

CONFIG_MOUNT_SCHEMA = "config-mount.schema.json"
CREATION_LIBRARY_BINDING_SCHEMA = "creation-library-binding.schema.json"
# Configuration record protocol, independent of the generating PAL release.
ADAPTER_VERSION = "0.1.0"
SUPPORTED_ADAPTER_VERSIONS = {"0.1.0", "0.2.0"}


def default_config_root() -> Path:
    """Return the platform-specific PAL user configuration root."""

    if sys.platform.startswith("win"):
        configured = os.environ.get("LOCALAPPDATA")
        return (Path(configured) if configured else Path.home() / "AppData" / "Local") / "pal"
    if os.name != "posix":
        raise PathSafetyError("PAL requires macOS, Linux or Windows")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "pal"
    configured = os.environ.get("XDG_CONFIG_HOME")
    base = Path(configured) if configured else Path.home() / ".config"
    return base / "pal"


def resolve_config_root(
    config_root: Path | None = None,
    *,
    create: bool,
) -> Path:
    """Resolve an explicit or platform-default PAL configuration root safely."""

    controlled = os.environ.get("PAL_CONFIG_ROOT") if config_root is None else None
    selected = config_root or (Path(controlled) if controlled else default_config_root())
    return _absolute_no_symlink_root(selected, create=create)


def _absolute_no_symlink_root(path: Path, *, create: bool) -> Path:
    """Canonicalize a config root while rejecting symlinks at every level."""

    validate_windows_path(path)
    lexical = Path(os.path.abspath(path))
    validate_windows_path(lexical)
    # macOS exposes common temporary/user locations (notably ``/tmp`` and
    # ``/var``) as system symlinks.  Canonicalize those stable prefixes first;
    # the requested leaf itself is still rejected when it is a symlink.
    if lexical.exists() and is_link(lexical):
        raise PathSafetyError(f"PAL config root cannot be a symbolic link: {lexical}")
    # macOS's standard aliases are safe because they resolve to the same
    # system-owned tree; any user-created link below those aliases remains
    # rejected by this walk.
    allowed_system_aliases = (
        {Path("/tmp"), Path("/var"), Path("/etc"), Path("/home")}
        if sys.platform == "darwin"
        else set()
    )
    cursor = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        cursor /= part
        if is_link(cursor) and cursor not in allowed_system_aliases:
            raise PathSafetyError(f"PAL config root contains a symbolic link: {cursor}")
    absolute = Path(os.path.realpath(lexical))
    if absolute == Path(absolute.anchor) or not absolute.name:
        raise PathSafetyError("PAL config root cannot be a filesystem root")

    # Walk from the existing anchor to the requested leaf.  ``Path.resolve``
    # alone would silently follow an intermediate symlink, which would make a
    # mount record write outside the selected PAL configuration root.
    cursor = Path(absolute.anchor)
    missing: list[Path] = []
    for part in absolute.parts[1:]:
        cursor /= part
        if cursor.exists() or is_link(cursor):
            if is_link(cursor):
                raise PathSafetyError(f"PAL config root contains a symbolic link: {cursor}")
            if not cursor.is_dir():
                raise PathSafetyError(f"PAL config root component is not a directory: {cursor}")
        else:
            missing.append(cursor)

    if missing and not create:
        raise PathSafetyError(f"PAL config root does not exist: {absolute}")
    for directory in missing:
        try:
            directory.mkdir()
        except FileExistsError:
            if is_link(directory) or not directory.is_dir():
                raise PathSafetyError(
                    f"PAL config root component is not a real directory: {directory}"
                ) from None
    return absolute


def _mkdir_no_symlink(path: Path, root: Path) -> None:
    """Create a descendant directory without following symlink components."""

    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PathSafetyError(f"configuration path escapes config root: {path}") from exc
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.exists() or is_link(cursor):
            if is_link(cursor):
                raise PathSafetyError(f"configuration path contains a symbolic link: {cursor}")
            if not cursor.is_dir():
                raise PathSafetyError(f"configuration path is not a directory: {cursor}")
        else:
            cursor.mkdir()


def _config_mount_path(config_root: Path, library_id: str, cli_id: str) -> Path:
    require_safe_id(library_id, "library_id")
    if cli_id not in TARGET_CLIS:
        raise PathSafetyError(f"unsupported target CLI: {cli_id}")
    directory = config_root / "mounts" / "config" / library_id
    _mkdir_no_symlink(directory, config_root)
    return directory / f"{cli_id}.json"


def _absolute_reference(path: Path, root: Path, label: str) -> dict[str, str]:
    """Convert a validated in-library reference to an absolute record entry."""

    # ``_verify_reference`` has already checked the relative path and digest;
    # this additional boundary check makes the invariant explicit at the
    # mount-record layer.
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PathSafetyError(f"{label} escapes library root: {path}") from exc
    return {"path": str(path), "sha256": sha256_file(path)}


def _manifest_context(root: Path, manifest: dict[str, Any], cli_id: str) -> dict[str, Any]:
    """Build the canonical context fields from a freshly doctored library."""

    manifest_path = root / "library.json"
    references = manifest["specifications"]
    common: list[dict[str, str]] = []
    for index, reference in enumerate(references["common"]):
        path = _verify_reference(root, reference, f"specifications.common[{index}]")
        common.append(_absolute_reference(path, root, f"specifications.common[{index}]"))

    cli_references: dict[str, list[dict[str, str]]] = {}
    for target_cli in TARGET_CLIS:
        entries: list[dict[str, str]] = []
        for index, reference in enumerate(references["cli"][target_cli]):
            path = _verify_reference(
                root,
                reference,
                f"specifications.cli.{target_cli}[{index}]",
            )
            entries.append(
                _absolute_reference(path, root, f"specifications.cli.{target_cli}[{index}]")
            )
        cli_references[target_cli] = entries

    design_rules: list[dict[str, str]] = []
    for index, reference in enumerate(manifest["design_rules"]):
        path = _verify_reference(root, reference, f"design_rules[{index}]")
        design_rules.append(_absolute_reference(path, root, f"design_rules[{index}]"))

    profile_ids: dict[str, list[str]] = {target_cli: [] for target_cli in TARGET_CLIS}
    for profile in manifest["capability_profiles"]:
        covered = profile["covered_clis"]
        for target_cli in covered:
            if profile["profile_id"] not in profile_ids[target_cli]:
                profile_ids[target_cli].append(profile["profile_id"])

    expected = {
        "library_id": manifest["library_id"],
        "library_root": str(root),
        "library_manifest_sha256": sha256_file(manifest_path),
        "development_root": str(
            require_inside(root, manifest["development_root"], "development_root")
        ),
        "production_root": str(
            require_inside(root, manifest["production_root"], "production_root")
        ),
        "target_clis": TARGET_CLIS.copy(),
        "common_specifications": common,
        "cli_specifications": cli_references,
        "design_rules": design_rules,
        "profile_ids": profile_ids,
    }
    if cli_id not in expected["cli_specifications"]:
        raise PathSafetyError(f"unsupported target CLI: {cli_id}")
    return expected


def _build_mount_record(root: Path, manifest: dict[str, Any], cli_id: str) -> dict[str, Any]:
    context = _manifest_context(root, manifest, cli_id)
    record = {
        "schema_version": 1,
        "mount_kind": "configuration",
        "mount_id": f"config-{context['library_id']}-{cli_id}",
        "library_id": context["library_id"],
        "cli_id": cli_id,
        "library_root": context["library_root"],
        "library_manifest_sha256": context["library_manifest_sha256"],
        "development_root": context["development_root"],
        "production_root": context["production_root"],
        "target_clis": context["target_clis"],
        "references": {
            "common_specifications": context["common_specifications"],
            "cli_specifications": context["cli_specifications"][cli_id],
            "design_rules": context["design_rules"],
        },
        "profile_ids": context["profile_ids"][cli_id],
        "pal_version": __version__,
        "adapter_version": ADAPTER_VERSION,
        "verified_at": utc_now(),
    }
    validate_instance(CONFIG_MOUNT_SCHEMA, record)
    return record


def _record_reference(path_value: object, root: Path, label: str) -> Path:
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise PathSafetyError(f"{label} must be an absolute path")
    path = Path(path_value)
    # Reject lexical aliases such as ``/tmp/lib/../other`` and every symlink
    # component, then enforce the resolved root boundary.
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise PathSafetyError(f"{label} escapes library root: {path}") from exc
    if relative == ".":
        if path != root:
            raise PathSafetyError(f"{label} is not a canonical absolute path: {path}")
        return root
    checked = require_inside(root, relative, label)
    if str(checked) != path_value:
        raise PathSafetyError(f"{label} is not a canonical absolute path: {path}")
    return checked


def _expected_record_fields(
    record: dict[str, Any], root: Path, manifest: dict[str, Any]
) -> dict[str, Any]:
    cli_id = record.get("cli_id")
    if cli_id not in TARGET_CLIS:
        raise PathSafetyError(f"unsupported target CLI in configuration mount: {cli_id}")
    context = _manifest_context(root, manifest, cli_id)
    expected = {
        "schema_version": 1,
        "mount_kind": "configuration",
        "mount_id": f"config-{context['library_id']}-{cli_id}",
        "library_id": context["library_id"],
        "cli_id": cli_id,
        "library_root": context["library_root"],
        "library_manifest_sha256": context["library_manifest_sha256"],
        "development_root": context["development_root"],
        "production_root": context["production_root"],
        "target_clis": TARGET_CLIS.copy(),
        "references": {
            "common_specifications": context["common_specifications"],
            "cli_specifications": context["cli_specifications"][cli_id],
            "design_rules": context["design_rules"],
        },
        "profile_ids": context["profile_ids"][cli_id],
    }
    return expected


def validate_config_mount(
    record_path: Path,
    library_root: Path,
    *,
    expected_cli: str | None = None,
    require_writable_development: bool = True,
) -> dict[str, Any]:
    """Validate one persisted mount against the current library context."""

    if expected_cli is not None and expected_cli not in TARGET_CLIS:
        raise PathSafetyError(f"unsupported target CLI: {expected_cli}")
    root = canonical_existing_root(library_root)
    doctor_development_context(root, require_writable_development=require_writable_development)
    manifest = load_json_object(require_inside(root, "library.json", "library manifest"))
    record_path = Path(os.path.abspath(record_path))
    if is_link(record_path) or not record_path.is_file():
        raise PathSafetyError(f"configuration mount record is unavailable: {record_path}")
    record = load_json_object(record_path)
    validate_instance(CONFIG_MOUNT_SCHEMA, record)
    if record["adapter_version"] not in SUPPORTED_ADAPTER_VERSIONS:
        raise IntegrityError("unsupported configuration mount adapter_version")
    if expected_cli is not None and record["cli_id"] != expected_cli:
        raise IntegrityError("configuration mount CLI does not match the requested CLI")
    if record["library_id"] != manifest["library_id"]:
        raise IntegrityError("configuration mount library_id mismatch")

    _record_reference(record["library_root"], root, "configuration mount library_root")
    development = _record_reference(
        record["development_root"], root, "configuration mount development_root"
    )
    production = _record_reference(
        record["production_root"], root, "configuration mount production_root"
    )
    if development != require_inside(root, manifest["development_root"], "development_root"):
        raise IntegrityError("configuration mount development_root mismatch")
    if production != require_inside(root, manifest["production_root"], "production_root"):
        raise IntegrityError("configuration mount production_root mismatch")

    for index, reference in enumerate(record["references"]["common_specifications"]):
        _record_reference(reference["path"], root, f"mount common specification[{index}]")
    for index, reference in enumerate(record["references"]["cli_specifications"]):
        _record_reference(reference["path"], root, f"mount CLI specification[{index}]")
    for index, reference in enumerate(record["references"]["design_rules"]):
        _record_reference(reference["path"], root, f"mount design rule[{index}]")

    expected = _expected_record_fields(record, root, manifest)
    for key, value in expected.items():
        if record[key] != value:
            raise IntegrityError(f"configuration mount {key} does not match current library")
    return record


def mount_config(
    library_root: Path,
    cli_id: str,
    *,
    config_root: Path | None = None,
) -> dict[str, Any]:
    """Create or idempotently reuse one PAL configuration mount."""

    if cli_id not in TARGET_CLIS:
        raise PathSafetyError(f"unsupported target CLI: {cli_id}")
    root = canonical_existing_root(library_root)
    # Complete P0 validation happens before any PAL config write.
    doctor = doctor_library(root)
    manifest = load_json_object(require_inside(root, "library.json", "library manifest"))
    if doctor["library_id"] != manifest["library_id"]:  # defensive invariant
        raise IntegrityError("doctor/library manifest identity mismatch")

    destination_root = resolve_config_root(config_root, create=True)
    destination = _config_mount_path(destination_root, manifest["library_id"], cli_id)
    record = _build_mount_record(root, manifest, cli_id)

    if destination.exists() or is_link(destination):
        if is_link(destination) or not destination.is_file():
            raise PathSafetyError(
                f"configuration mount destination is not a regular file: {destination}"
            )
        existing = validate_config_mount(destination, root, expected_cli=cli_id)
        # Generator metadata is provenance. Keep old bytes and binding hashes.
        stable_fields = set(record) - {"verified_at", "pal_version", "adapter_version"}
        if any(existing[key] != record[key] for key in stable_fields):
            raise IntegrityError("existing configuration mount differs from requested mount")
        return {
            "library_id": manifest["library_id"],
            "cli_id": cli_id,
            "mount_id": existing["mount_id"],
            "path": str(destination),
            "idempotent": True,
        }

    try:
        write_new_json(destination, record)
        fsync_directory(destination.parent)
    except FileExistsError:
        # A concurrent identical mount may win the O_EXCL race.  Re-read it
        # through the complete validator; a different record is rejected.
        existing = validate_config_mount(destination, root, expected_cli=cli_id)
        return {
            "library_id": manifest["library_id"],
            "cli_id": cli_id,
            "mount_id": existing["mount_id"],
            "path": str(destination),
            "idempotent": True,
        }

    return {
        "library_id": manifest["library_id"],
        "cli_id": cli_id,
        "mount_id": record["mount_id"],
        "path": str(destination),
        "idempotent": False,
    }


def resolve_creation_context(
    library_root: Path,
    *,
    config_root: Path | None = None,
    require_writable_development: bool = True,
) -> dict[str, Any]:
    """Resolve both CLI mounts into a validated, read-only P1 context."""

    root = canonical_existing_root(library_root)
    doctor_development_context(root, require_writable_development=require_writable_development)
    manifest = load_json_object(require_inside(root, "library.json", "library manifest"))
    destination_root = resolve_config_root(config_root, create=False)
    mounts: dict[str, dict[str, Any]] = {}
    for cli_id in TARGET_CLIS:
        path = destination_root / "mounts" / "config" / manifest["library_id"] / f"{cli_id}.json"
        # Do not create directories or records during a read-only resolution.
        mounts[cli_id] = validate_config_mount(
            path,
            root,
            expected_cli=cli_id,
            require_writable_development=require_writable_development,
        )

    common = mounts[TARGET_CLIS[0]]["references"]["common_specifications"]
    design_rules = mounts[TARGET_CLIS[0]]["references"]["design_rules"]
    return {
        "library_id": manifest["library_id"],
        "library_root": str(root),
        "development_root": mounts[TARGET_CLIS[0]]["development_root"],
        "production_root": mounts[TARGET_CLIS[0]]["production_root"],
        "library_manifest_sha256": mounts[TARGET_CLIS[0]]["library_manifest_sha256"],
        "target_clis": TARGET_CLIS.copy(),
        "references": {
            "common_specifications": common,
            "cli_specifications": {
                cli_id: mounts[cli_id]["references"]["cli_specifications"] for cli_id in TARGET_CLIS
            },
            "design_rules": design_rules,
        },
        "profile_ids": {cli_id: mounts[cli_id]["profile_ids"] for cli_id in TARGET_CLIS},
        "mounts": mounts,
    }


def _creation_library_binding_path(config_root: Path, *, create: bool) -> Path:
    directory = config_root / "defaults"
    if create:
        _mkdir_no_symlink(directory, config_root)
    elif is_link(directory) or (directory.exists() and not directory.is_dir()):
        raise PathSafetyError(f"default binding path is not a real directory: {directory}")
    return directory / "creation-library.json"


def bind_default_creation_library(
    library_root: Path,
    *,
    config_root: Path | None = None,
) -> dict[str, Any]:
    """Select one fully mounted library for ordinary-session creation."""

    root = canonical_existing_root(library_root)
    context = resolve_creation_context(root, config_root=config_root)
    destination_root = resolve_config_root(config_root, create=True)
    destination = _creation_library_binding_path(destination_root, create=True)
    mount_references = {
        cli_id: {
            "path": str(
                destination_root / "mounts" / "config" / context["library_id"] / f"{cli_id}.json"
            ),
            "sha256": sha256_file(
                destination_root / "mounts" / "config" / context["library_id"] / f"{cli_id}.json"
            ),
        }
        for cli_id in TARGET_CLIS
    }
    record = {
        "schema_version": 1,
        "binding_kind": "default-creation-library",
        "library_id": context["library_id"],
        "library_root": str(root),
        "config_mounts": mount_references,
        "pal_version": __version__,
        "selected_at": utc_now(),
    }
    validate_config_instance(CREATION_LIBRARY_BINDING_SCHEMA, record)

    if destination.exists() or is_link(destination):
        if is_link(destination) or not destination.is_file():
            raise PathSafetyError(f"default creation binding is not a regular file: {destination}")
        existing = load_json_object(destination)
        validate_config_instance(CREATION_LIBRARY_BINDING_SCHEMA, existing)
        stable_fields = set(record) - {"selected_at", "pal_version"}
        if all(existing[field] == record[field] for field in stable_fields):
            return {
                "library_id": context["library_id"],
                "library_root": str(root),
                "path": str(destination),
                "idempotent": True,
            }

    atomic_replace_json(destination, record)
    return {
        "library_id": context["library_id"],
        "library_root": str(root),
        "path": str(destination),
        "idempotent": False,
    }


def resolve_default_creation_library(
    *,
    config_root: Path | None = None,
) -> Path | None:
    """Resolve and revalidate the Quickstart-selected creation library."""

    controlled = os.environ.get("PAL_CONFIG_ROOT") if config_root is None else None
    selected = config_root or (Path(controlled) if controlled else default_config_root())
    lexical = Path(os.path.abspath(selected))
    if not lexical.exists() and not is_link(lexical):
        return None
    destination_root = resolve_config_root(config_root, create=False)
    binding_path = _creation_library_binding_path(destination_root, create=False)
    if not binding_path.exists() and not is_link(binding_path):
        return None
    if is_link(binding_path) or not binding_path.is_file():
        raise PathSafetyError(f"default creation binding is not a regular file: {binding_path}")
    record = load_json_object(binding_path)
    validate_config_instance(CREATION_LIBRARY_BINDING_SCHEMA, record)
    root = canonical_existing_root(Path(record["library_root"]))
    # Resolving the selected library is also used by read-only discovery
    # commands (for example ``pal create list`` from an ordinary CLI
    # workspace).  The binding lookup must validate identity and mount
    # integrity, but it must not require the development tree to be writable;
    # mutating operations perform their own writable-context check before any
    # transaction is opened.
    context = resolve_creation_context(
        root,
        config_root=destination_root,
        require_writable_development=False,
    )
    if context["library_id"] != record["library_id"] or context["library_root"] != str(root):
        raise IntegrityError("default creation binding library identity has drifted")
    for cli_id in TARGET_CLIS:
        expected_path = (
            destination_root / "mounts" / "config" / context["library_id"] / f"{cli_id}.json"
        )
        reference = record["config_mounts"][cli_id]
        if reference["path"] != str(expected_path):
            raise IntegrityError(f"default creation binding {cli_id} mount path has drifted")
        if sha256_file(expected_path) != reference["sha256"]:
            raise IntegrityError(f"default creation binding {cli_id} mount digest has drifted")
    return root


__all__ = [
    "ADAPTER_VERSION",
    "CONFIG_MOUNT_SCHEMA",
    "CREATION_LIBRARY_BINDING_SCHEMA",
    "bind_default_creation_library",
    "default_config_root",
    "mount_config",
    "resolve_creation_context",
    "resolve_default_creation_library",
    "resolve_config_root",
    "validate_config_mount",
]
