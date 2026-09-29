"""Immutable logical-unit releases and production aggregate composition.

This module implements the lifecycle slice between P1 development commits and
production mounting.  It creates content-addressed, self-contained release and
production objects without reading or changing the active production pointer.

Traceability: PRD-RELEASE-001, PRD-RELEASE-002; ACC-007, ACC-011, ACC-012.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import __version__
from . import locking as fcntl
from .components import component_type_driver
from .domain import decode_format_v1_plugin
from .errors import IntegrityError, PathSafetyError, ProductionError, ReleaseError
from .io import (
    canonical_json_bytes,
    fsync_directory,
    fsync_tree,
    sha256_bytes,
    sha256_file,
    tree_digest,
    write_new_bytes,
    write_new_json,
)
from .library import doctor_development_context, load_json_object, utc_now
from .maintenance import lifecycle_write
from .paths import (
    canonical_existing_root,
    normalize_relative_path,
    require_inside,
    require_safe_id,
    validate_regular_tree,
)
from .platform_support import is_link, move_path
from .schema_catalog import TARGET_CLIS, validate_instance


def _file_entries(root: Path) -> list[dict[str, str]]:
    validate_regular_tree(root)
    entries = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": sha256_file(path),
        }
        for path in root.rglob("*")
        if path.is_file()
    ]
    return sorted(entries, key=lambda item: item["path"].encode("utf-8"))


def _validate_inventory(
    payload_root: Path,
    entries: object,
    expected_tree_sha256: object,
    label: str,
    *,
    allow_empty: bool = False,
) -> list[dict[str, str]]:
    if is_link(payload_root) or not payload_root.is_dir():
        raise IntegrityError(f"{label} payload root is not a real directory: {payload_root}")
    if not isinstance(entries, list) or (not entries and not allow_empty):
        raise IntegrityError(f"{label} file inventory must be non-empty")
    expected: dict[str, str] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise IntegrityError(f"{label} file entry {index} is invalid")
        relative = normalize_relative_path(entry["path"], f"{label} file path")
        if relative in expected:
            raise IntegrityError(f"{label} file inventory contains a duplicate: {relative}")
        path = require_inside(payload_root, relative, f"{label} file")
        if not path.is_file():
            raise IntegrityError(f"{label} inventory entry is not a regular file: {path}")
        actual_digest = sha256_file(path)
        if actual_digest != entry["sha256"]:
            raise IntegrityError(f"{label} file SHA-256 mismatch: {relative}")
        expected[relative] = entry["sha256"]

    actual_entries = _file_entries(payload_root)
    actual = {entry["path"]: entry["sha256"] for entry in actual_entries}
    if actual != expected:
        raise IntegrityError(f"{label} file inventory differs from its payload tree")
    actual_tree_sha256 = tree_digest(payload_root)
    if actual_tree_sha256 != expected_tree_sha256:
        raise IntegrityError(f"{label} tree SHA-256 mismatch")
    return actual_entries


def _reference(path: Path, root: Path) -> dict[str, str]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256_file(path),
    }


def _copy_files(source_root: Path, files: Sequence[dict[str, str]], target_root: Path) -> None:
    for entry in files:
        source = require_inside(source_root, entry["path"], "immutable source payload file")
        if sha256_file(source) != entry["sha256"]:
            raise IntegrityError(f"immutable source payload drifted: {source}")
        write_new_bytes(target_root / entry["path"], source.read_bytes())


@contextmanager
def library_lock(root: Path, name: str, label: str) -> Iterator[None]:
    """Serialize one named library-scoped mutation inside the PAL lock root.

    Distinct names are independent, so a caller already holding one name can
    still take another without self-deadlock.
    """

    if fcntl is None:  # pragma: no cover - doctor already rejects this platform
        raise ReleaseError(f"{label} requires a supported file lock backend")
    lock_parent = require_inside(root, ".pal/locks", "PAL lock root")
    lock_path = lock_parent / f"{name}.lock"
    if is_link(lock_path):
        raise PathSafetyError(f"{label} lock cannot be a symbolic link: {lock_path}")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o644)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReleaseError(f"{label} is locked by another operation") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _safe_remove_temporary(path: Path, parent: Path, prefix: str) -> None:
    try:
        path.relative_to(parent)
    except ValueError as exc:  # pragma: no cover - defensive invariant
        raise PathSafetyError(f"refusing to clean temporary publishing path: {path}") from exc
    if is_link(path) or not path.name.startswith(prefix):
        raise PathSafetyError(f"refusing to clean unexpected publishing path: {path}")
    shutil.rmtree(path)


def _validate_artifact(
    manifest_path: Path,
    *,
    manifest_sha256: str,
    unit_id: str,
    revision_id: str,
    allowed_root: Path,
    label: str,
) -> dict[str, Any]:
    if is_link(manifest_path) or not manifest_path.is_file():
        raise IntegrityError(f"{label} manifest is unavailable: {manifest_path}")
    if sha256_file(manifest_path) != manifest_sha256:
        raise IntegrityError(f"{label} manifest SHA-256 mismatch")
    try:
        manifest_path.relative_to(allowed_root)
    except ValueError as exc:
        raise PathSafetyError(f"{label} manifest escapes its immutable object") from exc

    artifact = load_json_object(manifest_path)
    validate_instance("artifact.schema.json", artifact)
    if artifact["unit_id"] != unit_id or artifact["revision_id"] != revision_id:
        raise IntegrityError(f"{label} source identity mismatch")
    payload_root = require_inside(
        manifest_path.parent,
        artifact["payload_root"],
        f"{label} payload root",
    )
    files = _validate_inventory(
        payload_root,
        artifact["files"],
        artifact["tree_sha256"],
        label,
    )
    files_by_path = {entry["path"]: entry["sha256"] for entry in files}
    for dependency in artifact["dependencies"]:
        if files_by_path.get(dependency["path"]) != dependency["sha256"]:
            dependency_id = dependency["dependency_id"]
            raise IntegrityError(
                f"{label} dependency is not closed inside its payload: {dependency_id}"
            )
    component_type_driver(artifact["kind"]).validate_payload(artifact, payload_root)
    return {
        "artifact": artifact,
        "manifest_path": manifest_path,
        "manifest_sha256": manifest_sha256,
        "payload_root": payload_root,
        "files": files,
    }


def _validate_coverage(
    plugin_id: str,
    artifacts: Sequence[dict[str, Any]],
    label: str,
) -> None:
    artifact_ids: set[str] = set()
    domain_artifacts: list[dict[str, Any]] = []
    for item in artifacts:
        artifact = item["artifact"]
        component_type_driver(artifact["kind"])
        if artifact["artifact_id"] in artifact_ids:
            raise IntegrityError(f"{label} contains a duplicate artifact ID")
        artifact_ids.add(artifact["artifact_id"])
        domain_artifacts.append(artifact)
    decode_format_v1_plugin(plugin_id, domain_artifacts)


def _load_development_revision(
    root: Path,
    unit_id: str,
    revision_id: str,
    *,
    require_writable_development: bool = True,
) -> dict[str, Any]:
    doctor_development_context(root, require_writable_development=require_writable_development)
    unit_root = require_inside(root, f"development/units/{unit_id}", "logical unit")
    if not unit_root.is_dir():
        raise ReleaseError(f"logical unit is not a directory: {unit_id}")
    unit = load_json_object(unit_root / "unit.json")
    validate_instance("unit.schema.json", unit)
    unit_driver = component_type_driver(unit["kind"])
    if unit["unit_id"] != unit_id or revision_id not in unit["revision_ids"]:
        raise ReleaseError(f"revision is not committed by logical unit {unit_id}: {revision_id}")

    revision_root = require_inside(
        unit_root,
        f"revisions/{revision_id}",
        "development revision",
    )
    revision_path = require_inside(revision_root, "revision.json", "revision manifest")
    revision = load_json_object(revision_path)
    validate_instance("development-revision.schema.json", revision)
    if revision["unit_id"] != unit_id or revision["revision_id"] != revision_id:
        raise IntegrityError("development revision identity mismatch")
    if revision["target_clis"] != TARGET_CLIS:
        raise IntegrityError("development revision target CLI set mismatch")

    artifacts: list[dict[str, Any]] = []
    for reference in revision["artifacts"]:
        expected_manifest_relative = f"artifacts/{reference['artifact_id']}/artifact.json"
        if reference["manifest"]["path"] != expected_manifest_relative:
            raise IntegrityError("development artifact manifest path is not canonical")
        manifest_path = require_inside(
            revision_root,
            reference["manifest"]["path"],
            f"development artifact {reference['artifact_id']}",
        )
        item = _validate_artifact(
            manifest_path,
            manifest_sha256=reference["manifest"]["sha256"],
            unit_id=unit_id,
            revision_id=revision_id,
            allowed_root=revision_root,
            label=f"development artifact {reference['artifact_id']}",
        )
        if item["artifact"]["artifact_id"] != reference["artifact_id"]:
            raise IntegrityError("development artifact reference identity mismatch")
        if item["artifact"]["kind"] != unit_driver.type_id:
            raise IntegrityError("development component type differs from its format-v1 Plugin")
        artifacts.append(item)
    _validate_coverage(unit_id, artifacts, "development revision")
    return {
        "unit_id": unit_id,
        "revision_id": revision_id,
        "revision_path": revision_path,
        "revision_sha256": sha256_file(revision_path),
        "artifacts": artifacts,
    }


def validate_development_revision(
    library_root: Path,
    unit_id: str,
    revision_id: str,
    *,
    require_writable_development: bool = True,
) -> dict[str, Any]:
    """Read a committed revision through the same checks used for release creation."""
    root = canonical_existing_root(library_root)
    require_safe_id(unit_id, "unit_id")
    require_safe_id(revision_id, "revision_id")
    return _load_development_revision(
        root,
        unit_id,
        revision_id,
        require_writable_development=require_writable_development,
    )


def _release_fingerprint(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "unit_id": source["unit_id"],
        "revision_id": source["revision_id"],
        "revision_sha256": source["revision_sha256"],
        "artifacts": [
            {
                "artifact_id": item["artifact"]["artifact_id"],
                "manifest_sha256": item["manifest_sha256"],
                "profile_id": item["artifact"]["profile_id"],
                "covered_clis": item["artifact"]["covered_clis"],
                "tree_sha256": item["artifact"]["tree_sha256"],
            }
            for item in source["artifacts"]
        ],
    }


def _release_id(source: dict[str, Any]) -> str:
    digest = sha256_bytes(canonical_json_bytes(_release_fingerprint(source)))
    return f"release-{digest[:16]}"


def _validate_release_at(
    root: Path,
    release_root: Path,
    *,
    expected_unit_id: str | None = None,
    expected_release_id: str | None = None,
) -> dict[str, Any]:
    if is_link(release_root) or not release_root.is_dir():
        raise IntegrityError(f"release root is not a real directory: {release_root}")
    validate_regular_tree(release_root)
    manifest_path = require_inside(release_root, "release.json", "release manifest")
    release = load_json_object(manifest_path)
    validate_instance("release.schema.json", release)
    unit_id = require_safe_id(release["unit_id"], "release unit_id")
    release_id = require_safe_id(release["release_id"], "release_id")
    if expected_unit_id is not None and unit_id != expected_unit_id:
        raise IntegrityError("release unit identity mismatch")
    if expected_release_id is not None and release_id != expected_release_id:
        raise IntegrityError("release ID mismatch")
    release_units_root = require_inside(root, "releases/units", "release units root")
    try:
        release_relative = release_root.relative_to(release_units_root)
    except ValueError as exc:
        raise PathSafetyError("release object is outside the release units root") from exc
    if release_relative.parts != (unit_id, release_id):
        raise IntegrityError("release directory identity mismatch")

    revision_id = release["source_revision"]["revision_id"]
    expected_source_path = f"development/units/{unit_id}/revisions/{revision_id}/revision.json"
    if release["source_revision"]["manifest"]["path"] != expected_source_path:
        raise IntegrityError("release source revision reference path mismatch")

    payload_root = require_inside(release_root, release["payload_root"], "release payload root")
    files = _validate_inventory(
        payload_root,
        release["files"],
        release["tree_sha256"],
        f"release {release_id}",
    )
    release_file_map = {entry["path"]: entry["sha256"] for entry in files}

    artifacts: list[dict[str, Any]] = []
    for snapshot in release["artifacts"]:
        expected_manifest_relative = f"payload/artifacts/{snapshot['artifact_id']}/artifact.json"
        if snapshot["manifest"]["path"] != expected_manifest_relative:
            raise IntegrityError("release artifact manifest path is not canonical")
        manifest_path_for_artifact = require_inside(
            release_root,
            snapshot["manifest"]["path"],
            f"release artifact {snapshot['artifact_id']}",
        )
        try:
            relative_manifest = manifest_path_for_artifact.relative_to(payload_root).as_posix()
        except ValueError as exc:  # pragma: no cover - guarded by the canonical path check
            raise PathSafetyError("release artifact manifest escapes the payload") from exc
        if release_file_map.get(relative_manifest) != snapshot["manifest"]["sha256"]:
            raise IntegrityError("release artifact manifest is absent from the release inventory")
        item = _validate_artifact(
            manifest_path_for_artifact,
            manifest_sha256=snapshot["manifest"]["sha256"],
            unit_id=unit_id,
            revision_id=revision_id,
            allowed_root=release_root,
            label=f"release artifact {snapshot['artifact_id']}",
        )
        artifact = item["artifact"]
        for field in ("artifact_id", "profile_id", "covered_clis", "tree_sha256"):
            if snapshot[field] != artifact[field]:
                raise IntegrityError(f"release artifact snapshot field drifted: {field}")
        artifacts.append(item)
    _validate_coverage(unit_id, artifacts, f"release {release_id}")

    dependencies = sorted(
        [
            {
                "path": (
                    f"artifacts/{item['artifact']['artifact_id']}/payload/{dependency['path']}"
                ),
                "sha256": dependency["sha256"],
            }
            for item in artifacts
            for dependency in item["artifact"]["dependencies"]
        ],
        key=lambda entry: entry["path"].encode("utf-8"),
    )
    if release["dependencies"] != dependencies:
        raise IntegrityError("release dependency closure mismatch")

    source = {
        "unit_id": unit_id,
        "revision_id": revision_id,
        "revision_sha256": release["source_revision"]["manifest"]["sha256"],
        "artifacts": artifacts,
    }
    if _release_id(source) != release_id:
        raise IntegrityError("release content-addressed ID mismatch")
    return {
        "release": release,
        "release_id": release_id,
        "unit_id": unit_id,
        "manifest_path": manifest_path,
        "manifest_sha256": sha256_file(manifest_path),
        "payload_root": payload_root,
        "artifacts": artifacts,
    }


def validate_release(library_root: Path, unit_id: str, release_id: str) -> dict[str, Any]:
    """Validate one immutable release without dereferencing development state."""

    root = canonical_existing_root(library_root)
    unit_id = require_safe_id(unit_id, "unit_id")
    release_id = require_safe_id(release_id, "release_id")
    release_root = require_inside(
        root,
        f"releases/units/{unit_id}/{release_id}",
        "release",
    )
    return _validate_release_at(
        root,
        release_root,
        expected_unit_id=unit_id,
        expected_release_id=release_id,
    )


def _release_result(validated: dict[str, Any], *, idempotent: bool) -> dict[str, Any]:
    return {
        "release_id": validated["release_id"],
        "unit_id": validated["unit_id"],
        "revision_id": validated["release"]["source_revision"]["revision_id"],
        "manifest": str(validated["manifest_path"]),
        "manifest_sha256": validated["manifest_sha256"],
        "tree_sha256": validated["release"]["tree_sha256"],
        "artifacts": [
            {
                "artifact_id": item["artifact"]["artifact_id"],
                "profile_id": item["artifact"]["profile_id"],
                "covered_clis": item["artifact"]["covered_clis"],
                "tree_sha256": item["artifact"]["tree_sha256"],
            }
            for item in validated["artifacts"]
        ],
        "idempotent": idempotent,
    }


@lifecycle_write
def create_release(library_root: Path, unit_id: str, revision_id: str) -> dict[str, Any]:
    """Create one content-addressed release from a committed development revision."""

    root = canonical_existing_root(library_root)
    unit_id = require_safe_id(unit_id, "unit_id")
    revision_id = require_safe_id(revision_id, "revision_id")
    releases_parent = require_inside(root, "releases/units", "release units root")

    with library_lock(root, f"unit-{unit_id}", f"release for {unit_id}"):
        source = _load_development_revision(root, unit_id, revision_id)
        release_id = _release_id(source)
        unit_release_root = releases_parent / unit_id
        if unit_release_root.exists() or is_link(unit_release_root):
            if is_link(unit_release_root) or not unit_release_root.is_dir():
                raise PathSafetyError(f"release unit root is invalid: {unit_release_root}")
        else:
            unit_release_root.mkdir()
            fsync_directory(releases_parent)

        target = unit_release_root / release_id
        if target.exists() or is_link(target):
            validated = _validate_release_at(
                root,
                target,
                expected_unit_id=unit_id,
                expected_release_id=release_id,
            )
            return _release_result(validated, idempotent=True)

        temporary = Path(
            tempfile.mkdtemp(
                dir=unit_release_root,
                prefix=f".pal-release-{release_id}-",
                suffix=".tmp",
            )
        )
        committed = False
        try:
            payload_root = temporary / "payload"
            artifact_snapshots: list[dict[str, Any]] = []
            dependencies: list[dict[str, str]] = []
            for item in source["artifacts"]:
                artifact = item["artifact"]
                artifact_root = payload_root / "artifacts" / artifact["artifact_id"]
                artifact_manifest_target = artifact_root / "artifact.json"
                write_new_bytes(artifact_manifest_target, item["manifest_path"].read_bytes())
                _copy_files(item["payload_root"], item["files"], artifact_root / "payload")
                artifact_snapshots.append(
                    {
                        "artifact_id": artifact["artifact_id"],
                        "manifest": _reference(artifact_manifest_target, temporary),
                        "profile_id": artifact["profile_id"],
                        "covered_clis": artifact["covered_clis"],
                        "tree_sha256": artifact["tree_sha256"],
                    }
                )
                dependencies.extend(
                    {
                        "path": (
                            f"artifacts/{artifact['artifact_id']}/payload/{dependency['path']}"
                        ),
                        "sha256": dependency["sha256"],
                    }
                    for dependency in artifact["dependencies"]
                )

            release = {
                "schema_version": 1,
                "release_id": release_id,
                "unit_id": unit_id,
                "source_revision": {
                    "revision_id": revision_id,
                    "manifest": _reference(source["revision_path"], root),
                },
                "artifacts": artifact_snapshots,
                "payload_root": "payload",
                "files": _file_entries(payload_root),
                "tree_sha256": tree_digest(payload_root),
                "dependencies": sorted(
                    dependencies,
                    key=lambda entry: entry["path"].encode("utf-8"),
                ),
                "created_at": utc_now(),
                "pal_version": __version__,
            }
            validate_instance("release.schema.json", release)
            write_new_json(temporary / "release.json", release)
            fsync_tree(temporary)
            confirmed_source = _load_development_revision(root, unit_id, revision_id)
            if _release_fingerprint(confirmed_source) != _release_fingerprint(source):
                raise IntegrityError("development revision changed while creating its release")
            move_path(temporary, target)
            fsync_directory(unit_release_root)
            committed = True
        finally:
            if not committed and temporary.exists():
                _safe_remove_temporary(temporary, unit_release_root, ".pal-release-")

        validated = _validate_release_at(
            root,
            target,
            expected_unit_id=unit_id,
            expected_release_id=release_id,
        )
        return _release_result(validated, idempotent=False)


def _find_release(root: Path, release_id: str) -> dict[str, Any]:
    units_root = require_inside(root, "releases/units", "release units root")
    validate_regular_tree(units_root)
    matches: list[Path] = []
    for unit_root in units_root.iterdir():
        if is_link(unit_root) or not unit_root.is_dir():
            raise IntegrityError(f"invalid entry in release units root: {unit_root}")
        require_safe_id(unit_root.name, "release unit directory")
        candidate = unit_root / release_id
        if candidate.exists() or is_link(candidate):
            matches.append(candidate)
    if not matches:
        raise ProductionError(f"release does not exist: {release_id}")
    if len(matches) != 1:
        raise ProductionError(f"release ID is ambiguous across logical units: {release_id}")
    return _validate_release_at(root, matches[0], expected_release_id=release_id)


def _production_fingerprint(releases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "target_clis": TARGET_CLIS,
        "releases": [
            {
                "unit_id": item["unit_id"],
                "release_id": item["release_id"],
                "manifest_sha256": item["manifest_sha256"],
                "tree_sha256": item["release"]["tree_sha256"],
                "artifacts": [
                    {
                        "artifact_id": artifact["artifact"]["artifact_id"],
                        "profile_id": artifact["artifact"]["profile_id"],
                        "covered_clis": artifact["artifact"]["covered_clis"],
                        "tree_sha256": artifact["artifact"]["tree_sha256"],
                    }
                    for artifact in item["artifacts"]
                ],
            }
            for item in releases
        ],
    }


def _production_version_id(releases: Sequence[dict[str, Any]], schema_version: int = 2) -> str:
    fingerprint = _production_fingerprint(releases)
    if schema_version == 2:
        fingerprint["schema_version"] = 2
    digest = sha256_bytes(canonical_json_bytes(fingerprint))
    return f"production-{digest[:16]}"


def _validate_production_at(
    root: Path,
    version_root: Path,
    *,
    expected_version_id: str | None = None,
) -> dict[str, Any]:
    if is_link(version_root) or not version_root.is_dir():
        raise IntegrityError(f"production version root is invalid: {version_root}")
    validate_regular_tree(version_root)
    manifest_path = require_inside(version_root, "production.json", "production manifest")
    production = load_json_object(manifest_path)
    validate_instance("production-version.schema.json", production)
    version_id = require_safe_id(production["production_version_id"], "production version ID")
    if expected_version_id is not None and version_id != expected_version_id:
        raise IntegrityError("production version identity mismatch")
    if version_root.name != version_id or production["target_clis"] != TARGET_CLIS:
        raise IntegrityError("production version directory or target CLI identity mismatch")

    payload_root = require_inside(version_root, production["payload_root"], "production payload")
    _validate_inventory(
        payload_root,
        production["files"],
        production["tree_sha256"],
        f"production version {version_id}",
        allow_empty=production["schema_version"] == 2 and not production["releases"],
    )

    releases: list[dict[str, Any]] = []
    seen_units: set[str] = set()
    seen_release_ids: set[str] = set()
    for reference in production["releases"]:
        release_manifest_path = require_inside(
            root,
            reference["manifest"]["path"],
            f"production release {reference['release_id']}",
        )
        if sha256_file(release_manifest_path) != reference["manifest"]["sha256"]:
            raise IntegrityError("production release manifest SHA-256 mismatch")
        validated = _validate_release_at(
            root,
            release_manifest_path.parent,
            expected_unit_id=reference["unit_id"],
            expected_release_id=reference["release_id"],
        )
        if validated["unit_id"] in seen_units or validated["release_id"] in seen_release_ids:
            raise IntegrityError("production version contains a duplicate unit or release")
        seen_units.add(validated["unit_id"])
        seen_release_ids.add(validated["release_id"])
        releases.append(validated)
    if releases != sorted(releases, key=lambda item: item["unit_id"].encode("utf-8")):
        raise IntegrityError("production release references are not in canonical unit order")

    expected_artifacts: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    expected_artifact_order: list[str] = []
    for release in releases:
        for artifact in release["artifacts"]:
            artifact_id = artifact["artifact"]["artifact_id"]
            if artifact_id in expected_artifacts:
                raise IntegrityError(f"duplicate artifact ID across releases: {artifact_id}")
            expected_artifacts[artifact_id] = (release, artifact)
            expected_artifact_order.append(artifact_id)

    if [item["artifact_id"] for item in production["artifacts"]] != expected_artifact_order:
        raise IntegrityError("production artifacts are not in canonical release order")

    seen_artifacts: set[str] = set()
    for snapshot in production["artifacts"]:
        artifact_id = snapshot["artifact_id"]
        if artifact_id in seen_artifacts or artifact_id not in expected_artifacts:
            raise IntegrityError(f"unexpected or duplicate production artifact: {artifact_id}")
        seen_artifacts.add(artifact_id)
        release, source = expected_artifacts[artifact_id]
        artifact = source["artifact"]
        expected_fields = {
            "release_id": release["release_id"],
            "profile_id": artifact["profile_id"],
            "covered_clis": artifact["covered_clis"],
            "tree_sha256": artifact["tree_sha256"],
            "payload_root": (f"payload/units/{release['unit_id']}/artifacts/{artifact_id}"),
        }
        if any(snapshot[field] != value for field, value in expected_fields.items()):
            raise IntegrityError(f"production artifact snapshot drifted: {artifact_id}")
        artifact_payload = require_inside(
            version_root,
            snapshot["payload_root"],
            f"production artifact {artifact_id}",
        )
        _validate_inventory(
            artifact_payload,
            artifact["files"],
            artifact["tree_sha256"],
            f"production artifact {artifact_id}",
        )
        component_type_driver(artifact["kind"]).validate_payload(artifact, artifact_payload)
    if seen_artifacts != set(expected_artifacts):
        raise IntegrityError("production artifact set differs from its release closure")
    if _production_version_id(releases, production["schema_version"]) != version_id:
        raise IntegrityError("production content-addressed ID mismatch")
    return {
        "production": production,
        "production_version_id": version_id,
        "manifest_path": manifest_path,
        "manifest_sha256": sha256_file(manifest_path),
        "payload_root": payload_root,
        "releases": releases,
    }


def validate_production_version(library_root: Path, version_id: str) -> dict[str, Any]:
    """Validate a production aggregate and its complete immutable release closure."""

    root = canonical_existing_root(library_root)
    version_id = require_safe_id(version_id, "production version ID")
    version_root = require_inside(
        root,
        f"production/versions/{version_id}",
        "production version",
        must_exist=False,
    )
    if not version_root.exists():
        # A historical snapshot referenced by an append-only usage record may
        # be removed from the user-visible production history while its
        # immutable bytes remain in the evidence archive.
        version_root = require_inside(
            root,
            f"records/usage/snapshots/{version_id}",
            "archived production version",
        )
    return _validate_production_at(root, version_root, expected_version_id=version_id)


def _production_result(validated: dict[str, Any], *, idempotent: bool) -> dict[str, Any]:
    return {
        "production_version_id": validated["production_version_id"],
        "manifest": str(validated["manifest_path"]),
        "manifest_sha256": validated["manifest_sha256"],
        "tree_sha256": validated["production"]["tree_sha256"],
        "release_ids": [item["release_id"] for item in validated["releases"]],
        "artifact_ids": [
            artifact["artifact_id"] for artifact in validated["production"]["artifacts"]
        ],
        "idempotent": idempotent,
    }


def _restore_archived_production(
    root: Path, archived: Path, target: Path, version_id: str
) -> dict[str, Any]:
    """Reuse validated original bytes under the caller's composition lock."""
    _validate_production_at(root, archived, expected_version_id=version_id)
    digest = tree_digest(archived)
    if target.exists() or is_link(target):
        validated = _validate_production_at(root, target, expected_version_id=version_id)
        if tree_digest(target) != digest:
            raise IntegrityError("生产内容与既有归档字节不一致")
        return _production_result(validated, idempotent=True)
    temporary = Path(tempfile.mkdtemp(dir=target.parent, prefix=".pal-production-", suffix=".tmp"))
    try:
        candidate = temporary / version_id
        shutil.copytree(archived, candidate, symlinks=True)
        _validate_production_at(root, candidate, expected_version_id=version_id)
        if tree_digest(candidate) != digest or tree_digest(archived) != digest:
            raise IntegrityError("生产归档在复用过程中发生变化")
        fsync_tree(candidate)
        move_path(candidate, target)
        fsync_directory(target.parent)
    finally:
        _safe_remove_temporary(temporary, target.parent, ".pal-production-")
    return _production_result(
        _validate_production_at(root, target, expected_version_id=version_id), idempotent=False
    )


@lifecycle_write
def compose_production(library_root: Path, release_ids: Sequence[str]) -> dict[str, Any]:
    """Compose one immutable production version from explicit existing releases."""

    root = canonical_existing_root(library_root)
    doctor_development_context(root)
    normalized_ids = [require_safe_id(item, "release_id") for item in release_ids]
    if len(normalized_ids) != len(set(normalized_ids)):
        raise ProductionError("production compose release IDs must be unique")

    releases = [_find_release(root, release_id) for release_id in normalized_ids]
    releases.sort(key=lambda item: item["unit_id"].encode("utf-8"))
    if len({item["unit_id"] for item in releases}) != len(releases):
        raise ProductionError("production compose accepts only one release per logical unit")
    artifact_ids = [
        artifact["artifact"]["artifact_id"]
        for release in releases
        for artifact in release["artifacts"]
    ]
    if len(artifact_ids) != len(set(artifact_ids)):
        raise ProductionError("production releases contain duplicate artifact IDs")

    version_id = _production_version_id(releases)
    versions_root = require_inside(root, "production/versions", "production versions root")
    with library_lock(root, "production-compose", "production composition"):
        target = versions_root / version_id
        archived = require_inside(
            root,
            f"records/usage/snapshots/{version_id}",
            "archived production version",
            must_exist=False,
        )
        if archived.exists() or is_link(archived):
            return _restore_archived_production(root, archived, target, version_id)
        if target.exists() or is_link(target):
            validated = _validate_production_at(
                root,
                target,
                expected_version_id=version_id,
            )
            return _production_result(validated, idempotent=True)

        temporary = Path(
            tempfile.mkdtemp(
                dir=versions_root,
                prefix=f".pal-production-{version_id}-",
                suffix=".tmp",
            )
        )
        committed = False
        try:
            payload_root = temporary / "payload"
            payload_root.mkdir()
            release_references: list[dict[str, Any]] = []
            artifact_snapshots: list[dict[str, Any]] = []
            for release in releases:
                release_references.append(
                    {
                        "release_id": release["release_id"],
                        "unit_id": release["unit_id"],
                        "manifest": _reference(release["manifest_path"], root),
                    }
                )
                for item in release["artifacts"]:
                    artifact = item["artifact"]
                    relative_payload = (
                        f"units/{release['unit_id']}/artifacts/{artifact['artifact_id']}"
                    )
                    _copy_files(
                        item["payload_root"],
                        item["files"],
                        payload_root / relative_payload,
                    )
                    artifact_snapshots.append(
                        {
                            "artifact_id": artifact["artifact_id"],
                            "release_id": release["release_id"],
                            "profile_id": artifact["profile_id"],
                            "covered_clis": artifact["covered_clis"],
                            "payload_root": f"payload/{relative_payload}",
                            "tree_sha256": artifact["tree_sha256"],
                        }
                    )

            production = {
                "schema_version": 2,
                "production_version_id": version_id,
                "target_clis": TARGET_CLIS.copy(),
                "releases": release_references,
                "artifacts": artifact_snapshots,
                "payload_root": "payload",
                "files": _file_entries(payload_root),
                "tree_sha256": tree_digest(payload_root),
                "composed_at": utc_now(),
                "pal_version": __version__,
            }
            validate_instance("production-version.schema.json", production)
            write_new_json(temporary / "production.json", production)
            fsync_tree(temporary)
            confirmed_releases = [
                _find_release(root, release["release_id"]) for release in releases
            ]
            confirmed_releases.sort(key=lambda item: item["unit_id"].encode("utf-8"))
            if _production_fingerprint(confirmed_releases) != _production_fingerprint(releases):
                raise IntegrityError("release closure changed during production composition")
            move_path(temporary, target)
            fsync_directory(versions_root)
            committed = True
        finally:
            if not committed and temporary.exists():
                _safe_remove_temporary(temporary, versions_root, ".pal-production-")

        validated = _validate_production_at(
            root,
            target,
            expected_version_id=version_id,
        )
        return _production_result(validated, idempotent=False)


__all__ = [
    "compose_production",
    "create_release",
    "library_lock",
    "validate_production_version",
    "validate_release",
]
