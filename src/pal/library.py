"""P0 library initialization and health validation.

Traceability: PRD-P0-001 through PRD-P0-005, PRD-TECH-001; ACC-001, ACC-012.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import PAL_FORMAT_VERSION, SCHEMA_VERSION, __version__
from . import locking as fcntl
from .components import component_type_driver_for_profile
from .errors import InitializationError, IntegrityError, PALError, SchemaValidationError
from .io import (
    formatted_json_bytes,
    fsync_directory,
    fsync_tree,
    require_digest,
    sha256_bytes,
    sha256_file,
    write_new_bytes,
    write_new_json,
)
from .paths import (
    canonical_existing_root,
    canonical_init_target,
    require_inside,
    require_safe_id,
    validate_regular_tree,
)
from .platform_support import is_link, move_path
from .schema_catalog import (
    PORTABLE_SCHEMA_CATALOG,
    SCHEMA_CATALOG,
    SCHEMA_FILENAMES,
    TARGET_CLIS,
    check_catalog,
    schema_bytes,
    validate_instance,
)

SCHEMA_REGISTRY_PATH = "schemas/v1/index.json"
COMMON_SPEC_PATH = "development/specifications/common/skill-v1.md"
CLAUDE_SPEC_PATH = "development/specifications/cli/claude-code/skill-v1.md"
CODEX_SPEC_PATH = "development/specifications/cli/codex/skill-v1.md"
DESIGN_RULES_PATH = "development/design-rules/skill-v1.md"
BASIC_PROFILE_PATH = "development/specifications/common/skill-md-v1-basic.profile.json"
CLAUDE_PROFILE_PATH = (
    "development/specifications/cli/claude-code/skill-md-v1-claude-model-pinned.profile.json"
)
REQUIRED_DIRECTORIES = (
    "schemas/v1",
    "development/specifications/common",
    "development/specifications/cli/claude-code",
    "development/specifications/cli/codex",
    "development/design-rules",
    "development/units",
    "releases/units",
    "production/versions",
    "records/usage",
    ".pal/transactions/creation",
    ".pal/transactions/usage",
    ".pal/locks",
)

COMMON_SPEC = """# PAL Skill common specification v1

Traceability: PRD-SPEC-001, PRD-SPEC-002, PRD-CREATE-002; ACC-002, ACC-005.

- A canonical Skill artifact contains one `SKILL.md` with `name` and `description` frontmatter.
- Artifact sharing is decided by an exact capability-profile match, never by the initiating CLI.
- Unknown fields, dependencies, or loading behavior fail closed instead of being silently removed.
"""

CLAUDE_SPEC = """# Claude Code Skill profile constraints v1

Traceability: PRD-SPEC-001, PRD-SPEC-002, PRD-CREATE-003; ACC-002, ACC-006.

- Protocol evidence captured on Claude Code `2.1.234`; runtime admission uses target probes.
- The shared basic profile is supported.
- The directed model-pinned profile adds the native string `model` frontmatter field.
- Plugin manifest and `--plugin-dir` discovery metadata are directed projection concerns.
"""

CODEX_SPEC = """# Codex Skill profile constraints v1

Traceability: PRD-SPEC-001, PRD-SPEC-002, PRD-CREATE-003; ACC-002, ACC-006.

- Protocol evidence captured on Codex `0.147.0`; runtime admission uses target probes.
- The shared basic profile is supported.
- The Claude `model` frontmatter field is not accepted by this profile.
- Plugin manifest, marketplace, and installation metadata are directed projection concerns.
"""

DESIGN_RULES = """# PAL initial Skill design rules

Traceability: PRD-CREATE-001 through PRD-CREATE-005; ACC-003 through ACC-006.

- One user request creates one logical Skill unit covering the complete target CLI set.
- Equivalent profiles share one canonical artifact; proven differences create directed artifacts.
- P1 commits only to development; it never creates a release, production version, or
  production mount.
- Runtime tool or script dependencies remain subordinate, self-contained Skill content.
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _require_supported_platform() -> None:
    if fcntl is None or not fcntl.SUPPORTED:
        raise InitializationError(
            "PAL requires the validated Windows/macOS/Linux filesystem and lock backend"
        )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SchemaValidationError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except FileNotFoundError as exc:
        raise SchemaValidationError(f"required JSON file is missing: {path}") from exc
    except UnicodeDecodeError as exc:
        raise SchemaValidationError(f"JSON file is not UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SchemaValidationError(f"invalid JSON in {path}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise SchemaValidationError(f"JSON root must be an object: {path}")
    return value


def _reference(path: Path, root: Path) -> dict[str, str]:
    return {"path": path.relative_to(root).as_posix(), "sha256": sha256_file(path)}


def _basic_profile() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_id": "skill-md-v1-basic",
        "artifact_kind": "skill",
        "extends": None,
        "covered_clis": TARGET_CLIS,
        "cli_versions": {"claude-code": "2.1.234", "codex": "0.147.0"},
        "canonical_path": "skills/<skill-name>/SKILL.md",
        "frontmatter_fields": [
            {"name": "name", "required": True, "value_type": "string"},
            {"name": "description", "required": True, "value_type": "string"},
        ],
        "name_constraints": {"pattern": "^[a-z0-9-]+$", "max_length": 64},
        "description_constraints": {
            "max_length": 1024,
            "forbid_angle_brackets": True,
        },
        "body_format": "markdown",
        "captured_at": "2026-08-18T11:50:00+08:00",
    }


def _claude_model_profile() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_id": "skill-md-v1-claude-model-pinned",
        "artifact_kind": "skill",
        "extends": "skill-md-v1-basic",
        "covered_clis": ["claude-code"],
        "cli_versions": {"claude-code": "2.1.234"},
        "canonical_path": "skills/<skill-name>/SKILL.md",
        "frontmatter_fields": [
            {"name": "name", "required": True, "value_type": "string"},
            {"name": "description", "required": True, "value_type": "string"},
            {"name": "model", "required": True, "value_type": "string"},
        ],
        "name_constraints": {"pattern": "^[a-z0-9-]+$", "max_length": 64},
        "description_constraints": {
            "max_length": 1024,
            "forbid_angle_brackets": True,
        },
        "body_format": "markdown",
        "captured_at": "2026-08-18T11:50:00+08:00",
    }


def _create_directories(root: Path) -> None:
    for relative in REQUIRED_DIRECTORIES:
        (root / relative).mkdir(parents=True, exist_ok=False)


def _write_schemas(root: Path) -> dict[str, Any]:
    entries: list[dict[str, str]] = []
    for filename in SCHEMA_FILENAMES:
        path = root / "schemas" / "v1" / filename
        material = schema_bytes(filename)
        write_new_bytes(path, material)
        entries.append(
            {
                "name": filename,
                "path": f"schemas/v1/{filename}",
                "$id": PORTABLE_SCHEMA_CATALOG[filename]["$id"],
                "sha256": sha256_bytes(material),
            }
        )
    registry = {
        "schema_version": SCHEMA_VERSION,
        "pal_format_version": PAL_FORMAT_VERSION,
        "generated_by_pal_version": __version__,
        "schemas": entries,
    }
    write_new_json(root / SCHEMA_REGISTRY_PATH, registry)
    return registry


def _write_seed_context(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    text_files = {
        COMMON_SPEC_PATH: COMMON_SPEC.encode("utf-8"),
        CLAUDE_SPEC_PATH: CLAUDE_SPEC.encode("utf-8"),
        CODEX_SPEC_PATH: CODEX_SPEC.encode("utf-8"),
        DESIGN_RULES_PATH: DESIGN_RULES.encode("utf-8"),
    }
    for relative, material in text_files.items():
        write_new_bytes(root / relative, material)

    basic = _basic_profile()
    claude = _claude_model_profile()
    validate_instance("capability-profile.schema.json", basic)
    validate_instance("capability-profile.schema.json", claude)
    write_new_json(root / BASIC_PROFILE_PATH, basic)
    write_new_json(root / CLAUDE_PROFILE_PATH, claude)
    return basic, claude


def _library_manifest(
    root: Path,
    library_id: str,
    created_at: str,
    basic_profile: dict[str, Any],
    claude_profile: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "library_id": library_id,
        "development_root": "development",
        "release_root": "releases",
        "production_root": "production",
        "usage_record_root": "records/usage",
        "schemas_root": "schemas/v1",
        "schema_registry": _reference(root / SCHEMA_REGISTRY_PATH, root),
        "target_clis": TARGET_CLIS,
        "specifications": {
            "common": [_reference(root / COMMON_SPEC_PATH, root)],
            "cli": {
                "claude-code": [_reference(root / CLAUDE_SPEC_PATH, root)],
                "codex": [_reference(root / CODEX_SPEC_PATH, root)],
            },
        },
        "design_rules": [_reference(root / DESIGN_RULES_PATH, root)],
        "capability_profiles": [
            {
                "profile_id": basic_profile["profile_id"],
                "covered_clis": basic_profile["covered_clis"],
                "cli_versions": basic_profile["cli_versions"],
                **_reference(root / BASIC_PROFILE_PATH, root),
            },
            {
                "profile_id": claude_profile["profile_id"],
                "covered_clis": claude_profile["covered_clis"],
                "cli_versions": claude_profile["cli_versions"],
                **_reference(root / CLAUDE_PROFILE_PATH, root),
            },
        ],
        "created_at": created_at,
        "pal_format_version": PAL_FORMAT_VERSION,
    }


def _active_pointer(library_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "library_id": library_id,
        "active_production_version_id": None,
        "production_manifest": None,
        "production_mount_bundle": None,
        "activated_at": None,
    }


def _build_library_tree(root: Path, library_id: str) -> None:
    _create_directories(root)
    _write_schemas(root)
    basic_profile, claude_profile = _write_seed_context(root)
    created_at = utc_now()
    manifest = _library_manifest(
        root,
        library_id,
        created_at,
        basic_profile,
        claude_profile,
    )
    active = _active_pointer(library_id)
    validate_instance("library.schema.json", manifest)
    validate_instance("active-production.schema.json", active)
    write_new_json(root / "library.json", manifest)
    write_new_json(root / "production" / "active.json", active)


def _safe_remove_staging(staging: Path, parent: Path) -> None:
    try:
        staging.relative_to(parent)
    except ValueError as exc:  # pragma: no cover - defensive invariant
        raise InitializationError(f"refusing to clean staging outside parent: {staging}") from exc
    if not staging.name.startswith(".pal-init-") or is_link(staging):
        raise InitializationError(f"refusing to clean unexpected staging path: {staging}")
    shutil.rmtree(staging)


def initialize_library(path: Path, library_id: str) -> dict[str, Any]:
    """Create a complete P0 library and commit it with one directory replace."""

    _require_supported_platform()
    check_catalog()
    library_id = require_safe_id(library_id, "library_id")
    target = canonical_init_target(path)
    if target.exists():
        if not target.is_dir():
            raise InitializationError(f"library root exists and is not a directory: {target}")
        if any(target.iterdir()):
            raise InitializationError(f"library root must be empty: {target}")

    staging = Path(
        tempfile.mkdtemp(prefix=f".pal-init-{library_id}-", suffix=".tmp", dir=target.parent)
    )
    committed = False
    try:
        _build_library_tree(staging, library_id)
        doctor_library(staging)
        fsync_tree(staging)
        if os.name == "nt" and target.exists():
            # rmdir only removes an empty directory, including after a concurrent change.
            target.rmdir()
        move_path(staging, target, replace=True)
        fsync_directory(target.parent)
        committed = True
    except (PALError, OSError):
        raise
    finally:
        if not committed and staging.exists():
            _safe_remove_staging(staging, target.parent)

    result = doctor_library(target)
    return {
        "library_id": library_id,
        "library_root": str(target),
        "schema_count": result["schema_count"],
        "pal_format_version": PAL_FORMAT_VERSION,
    }


def _verify_reference(root: Path, reference: object, label: str) -> Path:
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise IntegrityError(f"{label} must be an exact path/SHA-256 reference")
    path = require_inside(root, reference["path"], f"{label}.path")
    if not path.is_file():
        raise IntegrityError(f"{label} must reference a regular file: {path}")
    digest = reference["sha256"]
    if not isinstance(digest, str):
        raise IntegrityError(f"{label}.sha256 must be a string")
    require_digest(path, digest, label)
    return path


def _verify_schema_registry(root: Path, manifest: dict[str, Any]) -> None:
    registry_path = _verify_reference(root, manifest["schema_registry"], "schema_registry")
    registry = load_json_object(registry_path)
    expected_keys = {
        "schema_version",
        "pal_format_version",
        "generated_by_pal_version",
        "schemas",
    }
    if set(registry) != expected_keys:
        raise SchemaValidationError("schema registry fields do not match the v1 contract")
    if registry["schema_version"] != SCHEMA_VERSION:
        raise SchemaValidationError("unsupported schema registry version")
    if registry["pal_format_version"] != PAL_FORMAT_VERSION:
        raise SchemaValidationError("unsupported PAL format version in schema registry")
    entries = registry["schemas"]
    if not isinstance(entries, list) or len(entries) != len(SCHEMA_FILENAMES):
        raise SchemaValidationError("schema registry must contain exactly 14 entries")

    catalog = SCHEMA_CATALOG
    if (
        entries
        and isinstance(entries[0], dict)
        and entries[0].get("$id") == PORTABLE_SCHEMA_CATALOG[SCHEMA_FILENAMES[0]]["$id"]
    ):
        catalog = PORTABLE_SCHEMA_CATALOG
    names: list[str] = []
    identifiers: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"name", "path", "$id", "sha256"}:
            raise SchemaValidationError("schema registry entry fields are invalid")
        filename = entry["name"]
        if filename not in SCHEMA_CATALOG:
            raise SchemaValidationError(f"unknown registered schema: {filename}")
        if entry["path"] != f"schemas/v1/{filename}":
            raise SchemaValidationError(f"registered schema path is invalid: {filename}")
        if entry["$id"] != catalog[filename]["$id"]:
            raise SchemaValidationError(f"registered schema ID is invalid: {filename}")
        if entry["$id"] in identifiers:
            raise SchemaValidationError(f"duplicate registered schema ID: {entry['$id']}")
        identifiers.add(entry["$id"])
        schema_path = _verify_reference(
            root,
            {"path": entry["path"], "sha256": entry["sha256"]},
            f"schema {filename}",
        )
        if entry["sha256"] != sha256_bytes(formatted_json_bytes(catalog[filename])):
            raise IntegrityError(f"registered schema differs from PAL v1 catalog: {filename}")
        if load_json_object(schema_path) != catalog[filename]:
            raise IntegrityError(
                f"registered schema bytes decode to unexpected content: {filename}"
            )
        names.append(filename)
    if tuple(names) != SCHEMA_FILENAMES:
        raise SchemaValidationError("schema registry order does not match the v1 catalog")


def _verify_context_references(root: Path, manifest: dict[str, Any]) -> None:
    references: list[tuple[str, object]] = []
    specifications = manifest["specifications"]
    references.extend(
        (f"specifications.common[{index}]", reference)
        for index, reference in enumerate(specifications["common"])
    )
    for cli_id in TARGET_CLIS:
        references.extend(
            (f"specifications.cli.{cli_id}[{index}]", reference)
            for index, reference in enumerate(specifications["cli"][cli_id])
        )
    references.extend(
        (f"design_rules[{index}]", reference)
        for index, reference in enumerate(manifest["design_rules"])
    )
    for label, reference in references:
        _verify_reference(root, reference, label)

    expected_profiles = {
        "skill-md-v1-basic": _basic_profile(),
        "skill-md-v1-claude-model-pinned": _claude_model_profile(),
    }
    profile_ids: set[str] = set()
    for index, reference in enumerate(manifest["capability_profiles"]):
        path = _verify_reference(
            root,
            {"path": reference["path"], "sha256": reference["sha256"]},
            f"capability_profiles[{index}]",
        )
        profile = load_json_object(path)
        validate_instance("capability-profile.schema.json", profile)
        component_type_driver_for_profile(profile)
        profile_id = profile["profile_id"]
        if profile_id in profile_ids:
            raise IntegrityError(f"duplicate capability profile ID: {profile_id}")
        profile_ids.add(profile_id)
        if profile_id not in expected_profiles or profile != expected_profiles[profile_id]:
            raise IntegrityError(f"capability profile is not validated by PAL 0.1.0: {profile_id}")
        for field in ("profile_id", "covered_clis", "cli_versions"):
            if reference[field] != profile[field]:
                raise IntegrityError(
                    f"capability profile reference field {field} does not match {profile_id}"
                )
    if profile_ids != set(expected_profiles):
        raise IntegrityError("library capability profile set is incomplete or unsupported")


def _verify_active_pointer(root: Path, library_id: str) -> dict[str, Any]:
    active_path = require_inside(root, "production/active.json", "active production pointer")
    active = load_json_object(active_path)
    validate_instance("active-production.schema.json", active)
    if active["library_id"] != library_id:
        raise IntegrityError("active production pointer library_id mismatch")
    if active["active_production_version_id"] is not None:
        _verify_reference(root, active["production_manifest"], "production_manifest")
        bundle = active["production_mount_bundle"]
        bundle_path = Path(bundle["path"])
        if is_link(bundle_path) or not bundle_path.is_file():
            raise IntegrityError(f"production mount bundle is unavailable: {bundle_path}")
        require_digest(bundle_path, bundle["sha256"], "production mount bundle")
    return active


def _doctor_library(
    path: Path, *, check_active: bool, require_writable_development: bool = True
) -> dict[str, Any]:
    _require_supported_platform()
    check_catalog()
    root = canonical_existing_root(path)
    validate_regular_tree(root)
    for relative in REQUIRED_DIRECTORIES:
        directory = require_inside(root, relative, f"required directory {relative}")
        if not directory.is_dir():
            raise IntegrityError(f"required library path is not a directory: {relative}")

    manifest_path = require_inside(root, "library.json", "library manifest")
    manifest = load_json_object(manifest_path)
    validate_instance("library.schema.json", manifest)
    library_id = require_safe_id(manifest["library_id"], "library_id")
    _verify_schema_registry(root, manifest)
    _verify_context_references(root, manifest)
    active = _verify_active_pointer(root, library_id) if check_active else None

    development_root = require_inside(root, manifest["development_root"], "development_root")
    production_root = require_inside(root, manifest["production_root"], "production_root")
    if development_root == production_root:
        raise IntegrityError("development and production roots must differ")
    access_mode = os.R_OK | os.X_OK
    if require_writable_development:
        access_mode |= os.W_OK
    if not os.access(development_root, access_mode):
        access_label = "readable and writable" if require_writable_development else "readable"
        raise IntegrityError(f"development root is not {access_label}: {development_root}")

    result = {
        "library_id": library_id,
        "library_root": str(root),
        "schema_count": len(SCHEMA_FILENAMES),
        "target_clis": TARGET_CLIS,
        "library_manifest_sha256": sha256_file(manifest_path),
        "phase": "p0-foundation" if check_active else "development-context",
    }
    if active is not None:
        result["active_production_version_id"] = active["active_production_version_id"]
        from .skill_state import read_skill_state
        from .stages import current_production_id

        read_skill_state(root)

        result["published_production_version_id"] = current_production_id(root)
    return result


def doctor_library(path: Path, *, require_writable_development: bool = True) -> dict[str, Any]:
    """Validate the P0 foundation and any populated active pointer fail closed."""

    return _doctor_library(
        path,
        check_active=True,
        require_writable_development=require_writable_development,
    )


def doctor_development_context(
    path: Path, *, require_writable_development: bool = True
) -> dict[str, Any]:
    """Validate P1 inputs without reading or locking the production active pointer."""

    return _doctor_library(
        path,
        check_active=False,
        require_writable_development=require_writable_development,
    )


def validate_schema_document(
    root: Path,
    relative_path: str,
    schema_filename: str,
    expected_sha256: str,
) -> dict[str, Any]:
    """Validate one schema document's path, digest, and structure.

    This common envelope provides every formal schema with the same path-escape,
    digest-mismatch, unknown-field, missing-field, and version fail-closed behavior.
    """

    root = canonical_existing_root(root)
    path = require_inside(root, relative_path, f"{schema_filename} document")
    if not path.is_file():
        raise IntegrityError(f"schema document is not a regular file: {path}")
    require_digest(path, expected_sha256, f"{schema_filename} document")
    document = load_json_object(path)
    validate_instance(schema_filename, document)
    return document


def document_digest(document: dict[str, Any]) -> str:
    """Return the persisted-byte digest used by schema document tests and callers."""

    return sha256_bytes(formatted_json_bytes(document))
