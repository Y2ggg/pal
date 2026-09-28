"""Formal PAL JSON Schema catalogs.

The library catalog is the implementation source for the 14 schemas sealed by
the technical-design review. ``pal init`` materializes those exact schemas into
a new library's ``schemas/v1`` directory. Config-root records use a separate
catalog so extending PAL user configuration never changes existing library
bytes or the v1 doctor contract.

Traceability: PRD-P0-003, PRD-P0-004, PRD-P0-005, PRD-TECH-001; ACC-001.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from .errors import SchemaValidationError
from .io import formatted_json_bytes, sha256_bytes

DRAFT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_BASE_ID = "https://pal.local/schemas/v1/"
ID_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
RELATIVE_PATH_PATTERN = r"^(?!/)(?!.*(?:^|/)\.\.?(?:/|$))(?!.*//)(?!.*\\)[^\u0000-\u001f]+$"
ABSOLUTE_PATH_PATTERN = r"^/[^\u0000-\u001f]*$"
TARGET_CLIS = ["claude-code", "codex"]
SCHEMA_FILENAMES = (
    "library.schema.json",
    "unit.schema.json",
    "development-revision.schema.json",
    "artifact.schema.json",
    "release.schema.json",
    "production-version.schema.json",
    "active-production.schema.json",
    "config-mount.schema.json",
    "production-mount.schema.json",
    "production-transition.schema.json",
    "creation-transaction.schema.json",
    "usage-transaction.schema.json",
    "usage-record.schema.json",
    "capability-profile.schema.json",
)


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/$defs/{name}"}


def _object(required: list[str], properties: dict[str, Any], **keywords: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
        **keywords,
    }


def _array(item: dict[str, Any], *, minimum: int = 0) -> dict[str, Any]:
    value: dict[str, Any] = {"type": "array", "items": item}
    if minimum:
        value["minItems"] = minimum
    return value


def _common_defs() -> dict[str, Any]:
    reference = _object(
        ["path", "sha256"],
        {"path": _ref("relative_path"), "sha256": _ref("sha256")},
    )
    absolute_reference = _object(
        ["path", "sha256"],
        {"path": _ref("absolute_path"), "sha256": _ref("sha256")},
    )
    file_entry = _object(
        ["path", "sha256"],
        {"path": _ref("relative_path"), "sha256": _ref("sha256")},
    )
    return {
        "id": {"type": "string", "pattern": ID_PATTERN},
        "sha256": {"type": "string", "pattern": SHA256_PATTERN},
        "relative_path": {"type": "string", "pattern": RELATIVE_PATH_PATTERN},
        "absolute_path": {"type": "string", "pattern": ABSOLUTE_PATH_PATTERN},
        "timestamp": {"type": "string", "format": "date-time"},
        "cli_id": {"enum": TARGET_CLIS},
        "target_clis": {"const": TARGET_CLIS},
        "covered_clis": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"enum": TARGET_CLIS},
        },
        "reference": reference,
        "absolute_reference": absolute_reference,
        "reference_list": _array(_ref("reference"), minimum=1),
        "absolute_reference_list": _array(_ref("absolute_reference"), minimum=1),
        "file_entry": file_entry,
        "file_list": _array(_ref("file_entry"), minimum=1),
    }


def _schema(
    filename: str,
    title: str,
    traceability: str,
    required: list[str],
    properties: dict[str, Any],
    **keywords: Any,
) -> dict[str, Any]:
    return {
        "$schema": DRAFT,
        "$id": f"{SCHEMA_BASE_ID}{filename}",
        "$comment": f"Traceability: {traceability}",
        "title": title,
        **_object(required, properties, **keywords),
        "$defs": _common_defs(),
    }


def _library_schema() -> dict[str, Any]:
    specification_set = _object(
        ["common", "cli"],
        {
            "common": _ref("reference_list"),
            "cli": _object(
                TARGET_CLIS,
                {
                    "claude-code": _ref("reference_list"),
                    "codex": _ref("reference_list"),
                },
            ),
        },
    )
    profile_reference = _object(
        ["profile_id", "covered_clis", "cli_versions", "path", "sha256"],
        {
            "profile_id": _ref("id"),
            "covered_clis": _ref("covered_clis"),
            "cli_versions": _object(
                [],
                {
                    "claude-code": {"type": "string", "minLength": 1},
                    "codex": {"type": "string", "minLength": 1},
                },
                minProperties=1,
            ),
            "path": _ref("relative_path"),
            "sha256": _ref("sha256"),
        },
    )
    return _schema(
        "library.schema.json",
        "PAL library manifest v1",
        "PRD-P0-001 through PRD-P0-005; ACC-001",
        [
            "schema_version",
            "library_id",
            "development_root",
            "release_root",
            "production_root",
            "usage_record_root",
            "schemas_root",
            "schema_registry",
            "target_clis",
            "specifications",
            "design_rules",
            "capability_profiles",
            "created_at",
            "pal_format_version",
        ],
        {
            "schema_version": {"const": 1},
            "library_id": _ref("id"),
            "development_root": {"const": "development"},
            "release_root": {"const": "releases"},
            "production_root": {"const": "production"},
            "usage_record_root": {"const": "records/usage"},
            "schemas_root": {"const": "schemas/v1"},
            "schema_registry": _ref("reference"),
            "target_clis": _ref("target_clis"),
            "specifications": specification_set,
            "design_rules": _ref("reference_list"),
            "capability_profiles": _array(profile_reference, minimum=2) | {"uniqueItems": True},
            "created_at": _ref("timestamp"),
            "pal_format_version": {"const": "1.0"},
        },
    )


def _unit_schema() -> dict[str, Any]:
    return _schema(
        "unit.schema.json",
        "PAL logical unit v1",
        "PRD-P0-003, PRD-CREATE-004; ACC-001, ACC-003, ACC-004",
        [
            "schema_version",
            "unit_id",
            "kind",
            "current_revision_id",
            "revision_ids",
            "created_at",
            "updated_at",
        ],
        {
            "schema_version": {"const": 1},
            "unit_id": _ref("id"),
            "kind": {"const": "skill"},
            "current_revision_id": {"oneOf": [_ref("id"), {"type": "null"}]},
            "revision_ids": _array(_ref("id")) | {"uniqueItems": True},
            "created_at": _ref("timestamp"),
            "updated_at": _ref("timestamp"),
        },
        allOf=[
            {
                "if": {"properties": {"current_revision_id": {"type": "null"}}},
                "then": {"properties": {"revision_ids": {"maxItems": 0}}},
                "else": {"properties": {"revision_ids": {"minItems": 1}}},
            }
        ],
    )


def _development_revision_schema() -> dict[str, Any]:
    artifact_reference = _object(
        ["artifact_id", "manifest"],
        {"artifact_id": _ref("id"), "manifest": _ref("reference")},
    )
    request = _object(
        ["summary", "sha256"],
        {
            "summary": {"type": "string", "minLength": 1},
            "sha256": _ref("sha256"),
        },
    )
    return _schema(
        "development-revision.schema.json",
        "PAL development revision v1",
        "PRD-P0-003, PRD-CREATE-001 through PRD-CREATE-005; ACC-003 through ACC-006",
        [
            "schema_version",
            "revision_id",
            "creation_id",
            "unit_id",
            "request",
            "source_files",
            "artifacts",
            "created_by_cli",
            "target_clis",
            "specification_refs",
            "profile_refs",
            "created_at",
        ],
        {
            "schema_version": {"const": 1},
            "revision_id": _ref("id"),
            "creation_id": _ref("id"),
            "unit_id": _ref("id"),
            "request": request,
            "source_files": _ref("file_list"),
            "artifacts": _array(artifact_reference, minimum=1) | {"uniqueItems": True},
            "created_by_cli": _ref("cli_id"),
            "target_clis": _ref("target_clis"),
            "specification_refs": _ref("reference_list"),
            "profile_refs": _ref("reference_list"),
            "created_at": _ref("timestamp"),
        },
    )


def _artifact_schema() -> dict[str, Any]:
    dependency = _object(
        ["dependency_id", "kind", "path", "sha256"],
        {
            "dependency_id": _ref("id"),
            "kind": {"enum": ["tool", "script"]},
            "path": _ref("relative_path"),
            "sha256": _ref("sha256"),
        },
    )
    return _schema(
        "artifact.schema.json",
        "PAL Skill artifact v1",
        "PRD-P0-003, PRD-SPEC-002, PRD-CREATE-002, PRD-CREATE-003; ACC-005, ACC-006",
        [
            "schema_version",
            "artifact_id",
            "unit_id",
            "revision_id",
            "kind",
            "profile_id",
            "covered_clis",
            "payload_root",
            "files",
            "tree_sha256",
            "dependencies",
        ],
        {
            "schema_version": {"const": 1},
            "artifact_id": _ref("id"),
            "unit_id": _ref("id"),
            "revision_id": _ref("id"),
            "kind": {"const": "skill"},
            "profile_id": _ref("id"),
            "covered_clis": _ref("covered_clis"),
            "payload_root": _ref("relative_path"),
            "files": _ref("file_list"),
            "tree_sha256": _ref("sha256"),
            "dependencies": _array(dependency) | {"uniqueItems": True},
        },
    )


def _release_schema() -> dict[str, Any]:
    source_revision = _object(
        ["revision_id", "manifest"],
        {"revision_id": _ref("id"), "manifest": _ref("reference")},
    )
    artifact_snapshot = _object(
        ["artifact_id", "manifest", "profile_id", "covered_clis", "tree_sha256"],
        {
            "artifact_id": _ref("id"),
            "manifest": _ref("reference"),
            "profile_id": _ref("id"),
            "covered_clis": _ref("covered_clis"),
            "tree_sha256": _ref("sha256"),
        },
    )
    return _schema(
        "release.schema.json",
        "PAL logical-unit release v1",
        "PRD-RELEASE-001, PRD-RELEASE-002; ACC-007, ACC-011",
        [
            "schema_version",
            "release_id",
            "unit_id",
            "source_revision",
            "artifacts",
            "payload_root",
            "files",
            "tree_sha256",
            "dependencies",
            "created_at",
            "pal_version",
        ],
        {
            "schema_version": {"const": 1},
            "release_id": _ref("id"),
            "unit_id": _ref("id"),
            "source_revision": source_revision,
            "artifacts": _array(artifact_snapshot, minimum=1) | {"uniqueItems": True},
            "payload_root": _ref("relative_path"),
            "files": _ref("file_list"),
            "tree_sha256": _ref("sha256"),
            "dependencies": _array(_ref("file_entry")) | {"uniqueItems": True},
            "created_at": _ref("timestamp"),
            "pal_version": {"type": "string", "minLength": 1},
        },
    )


def _production_version_schema() -> dict[str, Any]:
    release_reference = _object(
        ["release_id", "unit_id", "manifest"],
        {
            "release_id": _ref("id"),
            "unit_id": _ref("id"),
            "manifest": _ref("reference"),
        },
    )
    artifact_snapshot = _object(
        [
            "artifact_id",
            "release_id",
            "profile_id",
            "covered_clis",
            "payload_root",
            "tree_sha256",
        ],
        {
            "artifact_id": _ref("id"),
            "release_id": _ref("id"),
            "profile_id": _ref("id"),
            "covered_clis": _ref("covered_clis"),
            "payload_root": _ref("relative_path"),
            "tree_sha256": _ref("sha256"),
        },
    )
    return _schema(
        "production-version.schema.json",
        "PAL production aggregate version v1",
        "PRD-RELEASE-001, PRD-RELEASE-002, PRD-MOUNT-003; ACC-007, ACC-008, ACC-011",
        [
            "schema_version",
            "production_version_id",
            "target_clis",
            "releases",
            "artifacts",
            "payload_root",
            "files",
            "tree_sha256",
            "composed_at",
            "pal_version",
        ],
        {
            "schema_version": {"const": 1},
            "production_version_id": _ref("id"),
            "target_clis": _ref("target_clis"),
            "releases": _array(release_reference, minimum=1) | {"uniqueItems": True},
            "artifacts": _array(artifact_snapshot, minimum=1) | {"uniqueItems": True},
            "payload_root": _ref("relative_path"),
            "files": _ref("file_list"),
            "tree_sha256": _ref("sha256"),
            "composed_at": _ref("timestamp"),
            "pal_version": {"type": "string", "minLength": 1},
        },
    )


def _active_production_schema() -> dict[str, Any]:
    return _schema(
        "active-production.schema.json",
        "PAL active production pointer v1",
        "PRD-P0-002, PRD-P0-005, PRD-MOUNT-003, PRD-MOUNT-004; ACC-001, ACC-008, ACC-011",
        [
            "schema_version",
            "library_id",
            "active_production_version_id",
            "production_manifest",
            "production_mount_bundle",
            "activated_at",
        ],
        {
            "schema_version": {"const": 1},
            "library_id": _ref("id"),
            "active_production_version_id": {"oneOf": [_ref("id"), {"type": "null"}]},
            "production_manifest": {"oneOf": [_ref("reference"), {"type": "null"}]},
            "production_mount_bundle": {"oneOf": [_ref("absolute_reference"), {"type": "null"}]},
            "activated_at": {"oneOf": [_ref("timestamp"), {"type": "null"}]},
        },
        oneOf=[
            {
                "properties": {
                    "active_production_version_id": {"type": "null"},
                    "production_manifest": {"type": "null"},
                    "production_mount_bundle": {"type": "null"},
                    "activated_at": {"type": "null"},
                }
            },
            {
                "properties": {
                    "active_production_version_id": _ref("id"),
                    "production_manifest": _ref("reference"),
                    "production_mount_bundle": _ref("absolute_reference"),
                    "activated_at": _ref("timestamp"),
                }
            },
        ],
    )


def _config_mount_schema() -> dict[str, Any]:
    references = _object(
        ["common_specifications", "cli_specifications", "design_rules"],
        {
            "common_specifications": _ref("absolute_reference_list"),
            "cli_specifications": _ref("absolute_reference_list"),
            "design_rules": _ref("absolute_reference_list"),
        },
    )
    return _schema(
        "config-mount.schema.json",
        "PAL configuration mount record v1",
        "PRD-P0-004, PRD-MOUNT-001, PRD-MOUNT-002; ACC-001, ACC-002, ACC-012",
        [
            "schema_version",
            "mount_kind",
            "mount_id",
            "library_id",
            "cli_id",
            "library_root",
            "library_manifest_sha256",
            "development_root",
            "production_root",
            "target_clis",
            "references",
            "profile_ids",
            "pal_version",
            "adapter_version",
            "verified_at",
        ],
        {
            "schema_version": {"const": 1},
            "mount_kind": {"const": "configuration"},
            "mount_id": _ref("id"),
            "library_id": _ref("id"),
            "cli_id": _ref("cli_id"),
            "library_root": _ref("absolute_path"),
            "library_manifest_sha256": _ref("sha256"),
            "development_root": _ref("absolute_path"),
            "production_root": _ref("absolute_path"),
            "target_clis": _ref("target_clis"),
            "references": references,
            "profile_ids": _array(_ref("id"), minimum=1) | {"uniqueItems": True},
            "pal_version": {"type": "string", "minLength": 1},
            "adapter_version": {"type": "string", "minLength": 1},
            "verified_at": _ref("timestamp"),
        },
    )


def _creation_library_binding_schema() -> dict[str, Any]:
    config_mounts = _object(
        TARGET_CLIS,
        {cli_id: _ref("absolute_reference") for cli_id in TARGET_CLIS},
    )
    return _schema(
        "creation-library-binding.schema.json",
        "PAL default creation library binding v1",
        "PRD-MOUNT-001, PRD-CREATE-001; ACC-002, ACC-003, ACC-004, ACC-012",
        [
            "schema_version",
            "binding_kind",
            "library_id",
            "library_root",
            "config_mounts",
            "pal_version",
            "selected_at",
        ],
        {
            "schema_version": {"const": 1},
            "binding_kind": {"const": "default-creation-library"},
            "library_id": _ref("id"),
            "library_root": _ref("absolute_path"),
            "config_mounts": config_mounts,
            "pal_version": {"type": "string", "minLength": 1},
            "selected_at": _ref("timestamp"),
        },
    )


def _production_mount_schema() -> dict[str, Any]:
    cli_record = _object(
        ["cli_id", "path", "sha256"],
        {
            "cli_id": _ref("cli_id"),
            "path": _ref("relative_path"),
            "sha256": _ref("sha256"),
        },
    )
    release_link = _object(
        ["release_id", "manifest_sha256"],
        {"release_id": _ref("id"), "manifest_sha256": _ref("sha256")},
    )
    artifact_link = _object(
        ["artifact_id", "profile_id", "covered_clis", "tree_sha256"],
        {
            "artifact_id": _ref("id"),
            "profile_id": _ref("id"),
            "covered_clis": _ref("covered_clis"),
            "tree_sha256": _ref("sha256"),
        },
    )
    return _schema(
        "production-mount.schema.json",
        "PAL production mount bundle or CLI record v1",
        "PRD-P0-005, PRD-MOUNT-003, PRD-MOUNT-004; ACC-001, ACC-008, ACC-012",
        [
            "schema_version",
            "mount_kind",
            "record_kind",
            "mount_id",
            "library_id",
            "production_version_id",
            "production_manifest_sha256",
            "pal_version",
            "verified_at",
        ],
        {
            "schema_version": {"const": 1},
            "mount_kind": {"const": "production"},
            "record_kind": {"enum": ["bundle", "cli"]},
            "mount_id": _ref("id"),
            "library_id": _ref("id"),
            "production_version_id": _ref("id"),
            "production_manifest_sha256": _ref("sha256"),
            "cli_records": _array(cli_record, minimum=2) | {"maxItems": 2, "uniqueItems": True},
            "cli_id": _ref("cli_id"),
            "cli_version": {"type": "string", "minLength": 1},
            "releases": _array(release_link, minimum=1) | {"uniqueItems": True},
            "artifacts": _array(artifact_link, minimum=1) | {"uniqueItems": True},
            "projection_root": _ref("absolute_path"),
            "projection_tree_sha256": _ref("sha256"),
            "effective_on": {"const": "new-session"},
            "pal_version": {"type": "string", "minLength": 1},
            "verified_at": _ref("timestamp"),
        },
        allOf=[
            {
                "if": {"properties": {"record_kind": {"const": "bundle"}}},
                "then": {
                    "required": ["cli_records"],
                    "not": {
                        "anyOf": [
                            {"required": ["cli_id"]},
                            {"required": ["projection_root"]},
                        ]
                    },
                },
                "else": {
                    "required": [
                        "cli_id",
                        "cli_version",
                        "releases",
                        "artifacts",
                        "projection_root",
                        "projection_tree_sha256",
                        "effective_on",
                    ],
                    "not": {"required": ["cli_records"]},
                },
            }
        ],
    )


def _production_transition_schema() -> dict[str, Any]:
    projection = _object(
        ["plugin_id", "marketplace_id", "path", "tree_sha256", "installed"],
        {
            "plugin_id": _ref("id"),
            "marketplace_id": {"oneOf": [_ref("id"), {"type": "null"}]},
            "path": _ref("absolute_path"),
            "tree_sha256": _ref("sha256"),
            "installed": {"type": "boolean"},
        },
    )
    cli_state = _object(
        ["old_projection", "target_projection", "install_status"],
        {
            "old_projection": {"oneOf": [projection, {"type": "null"}]},
            "target_projection": projection,
            "install_status": {
                "enum": ["unchanged", "offline-verified", "removing-old", "target-installed"]
            },
        },
    )
    process = _object(
        ["pid", "hostname"],
        {
            "pid": {"type": "integer", "minimum": 1},
            "hostname": {"type": "string", "minLength": 1},
        },
    )
    return _schema(
        "production-transition.schema.json",
        "PAL production transition v1",
        "PRD-RELEASE-002, PRD-MOUNT-003; ACC-008, ACC-011, ACC-012",
        [
            "schema_version",
            "transaction_id",
            "library_id",
            "from_version_id",
            "to_version_id",
            "phase",
            "old_active_sha256",
            "new_active_sha256",
            "clis",
            "recovery_action",
            "created_at",
            "updated_at",
            "process",
        ],
        {
            "schema_version": {"const": 1},
            "transaction_id": _ref("id"),
            "library_id": _ref("id"),
            "from_version_id": {"oneOf": [_ref("id"), {"type": "null"}]},
            "to_version_id": _ref("id"),
            "phase": {
                "enum": [
                    "PREPARING_TARGET",
                    "TARGET_VERIFIED",
                    "SWAPPING_PRIVATE_PROJECTION",
                    "ACTIVE_POINTER_COMMITTED",
                ]
            },
            "old_active_sha256": _ref("sha256"),
            "new_active_sha256": _ref("sha256"),
            "clis": _object(
                TARGET_CLIS,
                {"claude-code": cli_state, "codex": cli_state},
            ),
            "recovery_action": {"enum": ["restore-old", "complete-target", "none"]},
            "created_at": _ref("timestamp"),
            "updated_at": _ref("timestamp"),
            "process": process,
        },
    )


def _creation_transaction_schema() -> dict[str, Any]:
    return _schema(
        "creation-transaction.schema.json",
        "PAL creation transaction v1",
        "PRD-CREATE-001 through PRD-CREATE-005; ACC-003 through ACC-006, ACC-012",
        [
            "schema_version",
            "creation_id",
            "library_id",
            "created_by_cli",
            "state",
            "staging_root",
            "request",
            "target_clis",
            "profile_ids",
            "created_at",
            "updated_at",
            "error",
        ],
        {
            "schema_version": {"const": 1},
            "creation_id": _ref("id"),
            "library_id": _ref("id"),
            "created_by_cli": _ref("cli_id"),
            "state": {"enum": ["OPEN", "GENERATED", "VALIDATED", "COMMITTED", "ABORTED"]},
            "staging_root": _ref("absolute_path"),
            "request": _ref("reference"),
            "target_clis": _ref("target_clis"),
            "profile_ids": _array(_ref("id"), minimum=1) | {"uniqueItems": True},
            "created_at": _ref("timestamp"),
            "updated_at": _ref("timestamp"),
            "error": {
                "oneOf": [
                    _object(
                        ["code", "message"],
                        {
                            "code": _ref("id"),
                            "message": {"type": "string", "minLength": 1},
                        },
                    ),
                    {"type": "null"},
                ]
            },
        },
    )


def _usage_transaction_schema() -> dict[str, Any]:
    selection = _object(
        ["session_id", "event_index", "loaded_sha256"],
        {
            "session_id": {"type": "string", "minLength": 1},
            "event_index": {"type": "integer", "minimum": 0},
            "loaded_sha256": _ref("sha256"),
        },
    )
    return _schema(
        "usage-transaction.schema.json",
        "PAL controlled usage transaction v1",
        "PRD-USE-001, PRD-USE-002; ACC-009, ACC-010, ACC-012",
        [
            "schema_version",
            "usage_id",
            "library_id",
            "cli_id",
            "state",
            "task",
            "production_version_id",
            "production_manifest_sha256",
            "event_stream",
            "selection",
            "started_at",
            "updated_at",
        ],
        {
            "schema_version": {"const": 1},
            "usage_id": _ref("id"),
            "library_id": _ref("id"),
            "cli_id": _ref("cli_id"),
            "state": {"enum": ["OPEN", "SELECTED", "COMPLETED", "ABORTED"]},
            "task": _ref("reference"),
            "production_version_id": _ref("id"),
            "production_manifest_sha256": _ref("sha256"),
            "event_stream": _ref("reference"),
            "selection": {"oneOf": [selection, {"type": "null"}]},
            "started_at": _ref("timestamp"),
            "updated_at": _ref("timestamp"),
        },
    )


def _usage_record_schema() -> dict[str, Any]:
    task = _object(
        ["task_id", "input", "input_sha256"],
        {
            "task_id": _ref("id"),
            "input": {"type": "string", "minLength": 1},
            "input_sha256": _ref("sha256"),
        },
    )
    overall_result = _object(
        ["status", "output", "output_sha256", "process_exit_code", "error_code"],
        {
            "status": {"enum": ["succeeded", "failed", "interrupted"]},
            "output": {"type": "string", "minLength": 1},
            "output_sha256": _ref("sha256"),
            "process_exit_code": {"type": ["integer", "null"]},
            "error_code": {"oneOf": [_ref("id"), {"type": "null"}]},
        },
    )
    cli = _object(
        ["id", "version", "session_id"],
        {
            "id": _ref("cli_id"),
            "version": {"type": "string", "minLength": 1},
            "session_id": {"type": "string", "minLength": 1},
        },
    )
    logical_unit = _object(
        ["unit_id", "release_id"],
        {"unit_id": _ref("id"), "release_id": _ref("id")},
    )
    loaded_artifact = _object(
        ["artifact_id", "profile_id", "covered_clis", "sha256"],
        {
            "artifact_id": _ref("id"),
            "profile_id": _ref("id"),
            "covered_clis": _ref("covered_clis"),
            "sha256": _ref("sha256"),
        },
    )
    selection = _object(
        ["kind", "event_index", "loaded_relative_path", "loaded_sha256"],
        {
            "kind": {"const": "actual-skill-load"},
            "event_index": {"type": "integer", "minimum": 0},
            "loaded_relative_path": _ref("relative_path"),
            "loaded_sha256": _ref("sha256"),
        },
    )
    completion = _object(
        ["kind", "result_code", "process_exit_code", "signal", "output_sha256"],
        {
            "kind": {"enum": ["terminal-cli-event", "pal-recovery-envelope"]},
            "result_code": _ref("id"),
            "process_exit_code": {"type": ["integer", "null"]},
            "signal": {"type": ["string", "null"]},
            "output_sha256": _ref("sha256"),
        },
    )
    capture = _object(
        [
            "kind",
            "event_stream_sha256",
            "event_count",
            "mount_bundle_sha256",
            "production_manifest_sha256",
            "recorder_version",
        ],
        {
            "kind": {"const": "controlled-jsonl-runner"},
            "event_stream_sha256": _ref("sha256"),
            "event_count": {"type": "integer", "minimum": 1},
            "mount_bundle_sha256": _ref("sha256"),
            "production_manifest_sha256": _ref("sha256"),
            "recorder_version": {"type": "string", "minLength": 1},
        },
    )
    return _schema(
        "usage-record.schema.json",
        "PAL usage association record v1",
        "PRD-USE-002, PRD-P3-001; ACC-009 through ACC-012",
        [
            "schema_version",
            "usage_id",
            "task",
            "overall_result",
            "cli",
            "production_version_id",
            "logical_units",
            "loaded_artifacts",
            "evidence",
            "started_at",
            "completed_at",
        ],
        {
            "schema_version": {"const": 1},
            "usage_id": _ref("id"),
            "task": task,
            "overall_result": overall_result,
            "cli": cli,
            "production_version_id": _ref("id"),
            "logical_units": _array(logical_unit, minimum=1) | {"uniqueItems": True},
            "loaded_artifacts": _array(loaded_artifact, minimum=1) | {"uniqueItems": True},
            "evidence": _object(
                ["selection", "completion", "capture"],
                {"selection": selection, "completion": completion, "capture": capture},
            ),
            "started_at": _ref("timestamp"),
            "completed_at": _ref("timestamp"),
        },
    )


def _capability_profile_schema() -> dict[str, Any]:
    frontmatter_field = _object(
        ["name", "required", "value_type"],
        {
            "name": {"type": "string", "pattern": r"^[a-z][a-z0-9_-]*$"},
            "required": {"type": "boolean"},
            "value_type": {"const": "string"},
        },
    )
    name_constraints = _object(
        ["pattern", "max_length"],
        {
            "pattern": {"type": "string", "minLength": 1},
            "max_length": {"type": "integer", "minimum": 1},
        },
    )
    description_constraints = _object(
        ["max_length", "forbid_angle_brackets"],
        {
            "max_length": {"type": "integer", "minimum": 1},
            "forbid_angle_brackets": {"type": "boolean"},
        },
    )
    return _schema(
        "capability-profile.schema.json",
        "PAL CLI capability profile v1",
        "PRD-SPEC-001, PRD-SPEC-002, PRD-CREATE-002, PRD-CREATE-003; ACC-005, ACC-006, ACC-012",
        [
            "schema_version",
            "profile_id",
            "artifact_kind",
            "extends",
            "covered_clis",
            "cli_versions",
            "canonical_path",
            "frontmatter_fields",
            "name_constraints",
            "description_constraints",
            "body_format",
            "captured_at",
        ],
        {
            "schema_version": {"const": 1},
            "profile_id": _ref("id"),
            "artifact_kind": {"const": "skill"},
            "extends": {"oneOf": [_ref("id"), {"type": "null"}]},
            "covered_clis": _ref("covered_clis"),
            "cli_versions": _object(
                [],
                {
                    "claude-code": {"type": "string", "minLength": 1},
                    "codex": {"type": "string", "minLength": 1},
                },
                minProperties=1,
            ),
            "canonical_path": {"type": "string", "minLength": 1},
            "frontmatter_fields": _array(frontmatter_field, minimum=2) | {"uniqueItems": True},
            "name_constraints": name_constraints,
            "description_constraints": description_constraints,
            "body_format": {"const": "markdown"},
            "captured_at": _ref("timestamp"),
        },
    )


SCHEMA_CATALOG: dict[str, dict[str, Any]] = {
    schema["$id"].removeprefix(SCHEMA_BASE_ID): schema
    for schema in (
        _library_schema(),
        _unit_schema(),
        _development_revision_schema(),
        _artifact_schema(),
        _release_schema(),
        _production_version_schema(),
        _active_production_schema(),
        _config_mount_schema(),
        _production_mount_schema(),
        _production_transition_schema(),
        _creation_transaction_schema(),
        _usage_transaction_schema(),
        _usage_record_schema(),
        _capability_profile_schema(),
    )
}

CONFIG_ROOT_SCHEMA_CATALOG: dict[str, dict[str, Any]] = {
    "skill-state.schema.json": _schema(
        "skill-state.schema.json",
        "PAL Skill mount selection and pending deletion v1",
        "SLC-004/005; ACC-008/011/012",
        ["schema_version", "library_id", "disabled_unit_ids", "pending_deletion_unit_ids"],
        {
            "schema_version": {"const": 1},
            "library_id": _ref("id"),
            "disabled_unit_ids": {**_array(_ref("id")), "uniqueItems": True},
            "pending_deletion_unit_ids": {**_array(_ref("id")), "uniqueItems": True},
        },
    ),
    "skill-action.schema.json": _schema(
        "skill-action.schema.json",
        "PAL durable Skill deletion v1",
        "SLC-005; ACC-011/012",
        [
            "schema_version",
            "library_id",
            "library_root",
            "config_root",
            "unit_id",
            "production_version_id",
            "base_production_version_id",
            "cleanup",
        ],
        {
            "schema_version": {"const": 1},
            "library_id": _ref("id"),
            "library_root": _ref("absolute_path"),
            "config_root": _ref("absolute_path"),
            "unit_id": _ref("id"),
            "base_production_version_id": {"anyOf": [_ref("id"), {"type": "null"}]},
            "production_version_id": {"anyOf": [_ref("id"), {"type": "null"}]},
            "cleanup": {"type": ["object", "null"]},
        },
    ),
    "current-production.schema.json": _schema(
        "current-production.schema.json",
        "PAL current published content v1",
        "STG-001/002/006; PRD-P0-002; ACC-007/011/012",
        ["schema_version", "library_id", "production_version_id", "manifest_sha256"],
        {
            "schema_version": {"const": 1},
            "library_id": _ref("id"),
            "production_version_id": _ref("id"),
            "manifest_sha256": _ref("sha256"),
        },
    ),
    "permanent-deletion.schema.json": _schema(
        "permanent-deletion.schema.json",
        "PAL permanent deletion journal v1",
        "DEL-001/005; PRD-RELEASE-002; ACC-012",
        ["schema_version", "library_id", "library_root", "config_root", "plan"],
        {
            "schema_version": {"const": 1},
            "library_id": _ref("id"),
            "library_root": _ref("absolute_path"),
            "config_root": _ref("absolute_path"),
            "plan": _object(
                [
                    "kind",
                    "object_id",
                    "unit_id",
                    "versions",
                    "releases",
                    "creation_ids",
                    "revision_count",
                    "token",
                    "claude_cache_root",
                ],
                {
                    "kind": {"enum": ["skill", "snapshot"]},
                    "object_id": _ref("id"),
                    "unit_id": {"anyOf": [_ref("id"), {"type": "null"}]},
                    "revision_count": {"type": "integer", "minimum": 0},
                    "creation_ids": _array(_ref("id")),
                    "token": _ref("sha256"),
                    "claude_cache_root": {"anyOf": [_ref("absolute_path"), {"type": "null"}]},
                    "usage_references": _array(_ref("relative_path")),
                    "releases": _array(
                        _object(
                            ["unit_id", "release_id"],
                            {"unit_id": _ref("id"), "release_id": _ref("id")},
                        )
                    ),
                    "versions": _array(
                        _object(
                            [
                                "version_id",
                                "unit_ids",
                                "stable",
                                "materialized",
                                "cache_versions",
                            ],
                            {
                                "version_id": _ref("id"),
                                "unit_ids": _array(_ref("id")),
                                "stable": {"type": "boolean"},
                                "materialized": {"type": "boolean"},
                                "archive_required": {"type": "boolean"},
                                "archive_path": _ref("absolute_path"),
                                "tree_sha256": _ref("sha256"),
                                "cache_versions": _array(
                                    _object(
                                        ["directory", "files"],
                                        {
                                            "directory": _ref("id"),
                                            "files": _array(_ref("file_entry")),
                                        },
                                    )
                                ),
                            },
                            allOf=[
                                {
                                    "if": {
                                        "required": ["archive_required"],
                                        "properties": {"archive_required": {"const": True}},
                                    },
                                    "then": {"required": ["archive_path", "tree_sha256"]},
                                }
                            ],
                        )
                    ),
                },
            ),
        },
    ),
    "creation-update-commit.schema.json": _schema(
        "creation-update-commit.schema.json",
        "PAL update commit journal v1",
        "PSM-003; PRD-CREATE-004; ACC-006, ACC-012",
        ["schema_version", "creation_id", "base_unit_sha256", "revision_tree_sha256", "unit"],
        {
            "schema_version": {"const": 1},
            "creation_id": _ref("id"),
            "base_unit_sha256": _ref("sha256"),
            "revision_tree_sha256": _ref("sha256"),
            "unit": {"type": "object"},
        },
    ),
    "creation-library-binding.schema.json": _creation_library_binding_schema(),
    "production-history-trash.schema.json": _schema(
        "production-history-trash.schema.json",
        "PAL production snapshot trash marker v1",
        "PSM-008; PRD-RELEASE-002; ACC-008, ACC-012",
        [
            "schema_version",
            "library_id",
            "library_root",
            "production_version_id",
            "manifest_sha256",
            "deleted_at",
        ],
        {
            "schema_version": {"const": 1},
            "library_id": _ref("id"),
            "library_root": _ref("absolute_path"),
            "production_version_id": _ref("id"),
            "manifest_sha256": _ref("sha256"),
            "deleted_at": _ref("timestamp"),
        },
    ),
}


def _v2_production_schemas() -> dict[str, dict[str, Any]]:
    result = {}
    for filename in ("production-version.schema.json", "production-mount.schema.json"):
        schema = deepcopy(SCHEMA_CATALOG[filename])
        schema["$id"] = schema["$id"].replace("/v1/", "/v2/")
        schema["title"] = schema["title"].replace("v1", "v2")
        schema["properties"]["schema_version"] = {"const": 2}
        for field in ("releases", "artifacts"):
            schema["properties"][field].pop("minItems", None)
        schema["$defs"]["file_list"].pop("minItems", None)
        result[filename] = schema
    return result


# Existing library foundations remain byte-identical.
V2_SCHEMA_CATALOG = _v2_production_schemas()


def check_catalog() -> None:
    if tuple(SCHEMA_CATALOG) != SCHEMA_FILENAMES:
        raise SchemaValidationError("formal schema catalog order or membership is invalid")
    identifiers: set[str] = set()
    for filename, schema in SCHEMA_CATALOG.items():
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise SchemaValidationError(f"invalid formal schema {filename}: {exc.message}") from exc
        identifier = schema["$id"]
        if identifier in identifiers:
            raise SchemaValidationError(f"duplicate formal schema ID: {identifier}")
        identifiers.add(identifier)
    for filename, schema in (CONFIG_ROOT_SCHEMA_CATALOG | V2_SCHEMA_CATALOG).items():
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise SchemaValidationError(
                f"invalid config-root schema {filename}: {exc.message}"
            ) from exc
        identifier = schema["$id"]
        if identifier in identifiers:
            raise SchemaValidationError(f"duplicate formal schema ID: {identifier}")
        identifiers.add(identifier)


def schema_document(filename: str) -> dict[str, Any]:
    try:
        return deepcopy(SCHEMA_CATALOG[filename])
    except KeyError as exc:
        raise SchemaValidationError(f"unknown formal schema: {filename}") from exc


def schema_bytes(filename: str) -> bytes:
    return formatted_json_bytes(schema_document(filename))


def schema_digest(filename: str) -> str:
    return sha256_bytes(schema_bytes(filename))


def validate_instance(filename: str, instance: Any) -> None:
    schema = schema_document(filename)
    if isinstance(instance, dict) and instance.get("schema_version") == 2:
        schema = deepcopy(V2_SCHEMA_CATALOG.get(filename, schema))
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(instance)
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path) or "<root>"
        raise SchemaValidationError(
            f"{filename} validation failed at {location}: {exc.message}"
        ) from exc


def validate_config_instance(filename: str, instance: Any) -> None:
    try:
        schema = deepcopy(CONFIG_ROOT_SCHEMA_CATALOG[filename])
    except KeyError as exc:
        raise SchemaValidationError(f"unknown config-root schema: {filename}") from exc
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(instance)
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path) or "<root>"
        raise SchemaValidationError(
            f"{filename} validation failed at {location}: {exc.message}"
        ) from exc


check_catalog()
