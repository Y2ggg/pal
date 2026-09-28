"""Valid minimal documents for every formal v1 schema.

Traceability: PRD-P0-003, PRD-TECH-001; ACC-001, ACC-012.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pal.schema_catalog import TARGET_CLIS

DIGEST = "0" * 64
OTHER_DIGEST = "1" * 64
TIMESTAMP = "2026-08-14T00:00:00Z"


def reference(path: str = "payload/file.txt", digest: str = DIGEST) -> dict[str, str]:
    return {"path": path, "sha256": digest}


def absolute_reference(path: str = "/tmp/pal/file.json", digest: str = DIGEST) -> dict[str, str]:
    return {"path": path, "sha256": digest}


def projection(*, marketplace_id: str | None = "pal-market-v1") -> dict[str, Any]:
    return {
        "plugin_id": "pal-prod-v1",
        "marketplace_id": marketplace_id,
        "path": "/tmp/pal/projection",
        "tree_sha256": DIGEST,
        "installed": False,
    }


def schema_samples() -> dict[str, dict[str, Any]]:
    common_profile_ref = {
        "profile_id": "skill-md-v1-basic",
        "covered_clis": TARGET_CLIS.copy(),
        "cli_versions": {"claude-code": "2.1.234", "codex": "0.147.0"},
        **reference("development/specifications/common/basic.profile.json"),
    }
    claude_profile_ref = {
        "profile_id": "skill-md-v1-claude-model-pinned",
        "covered_clis": ["claude-code"],
        "cli_versions": {"claude-code": "2.1.234"},
        **reference(
            "development/specifications/cli/claude-code/model.profile.json",
            OTHER_DIGEST,
        ),
    }
    file_entry = reference("payload/SKILL.md")
    artifact_snapshot = {
        "artifact_id": "artifact-v1",
        "manifest": reference("artifacts/artifact-v1.json"),
        "profile_id": "skill-md-v1-basic",
        "covered_clis": TARGET_CLIS.copy(),
        "tree_sha256": DIGEST,
    }
    production_artifact = {
        "artifact_id": "artifact-v1",
        "release_id": "release-v1",
        "profile_id": "skill-md-v1-basic",
        "covered_clis": TARGET_CLIS.copy(),
        "payload_root": "payload/artifact-v1",
        "tree_sha256": DIGEST,
    }
    samples: dict[str, dict[str, Any]] = {
        "library.schema.json": {
            "schema_version": 1,
            "library_id": "library-v1",
            "development_root": "development",
            "release_root": "releases",
            "production_root": "production",
            "usage_record_root": "records/usage",
            "schemas_root": "schemas/v1",
            "schema_registry": reference("schemas/v1/index.json"),
            "target_clis": TARGET_CLIS.copy(),
            "specifications": {
                "common": [reference("development/specifications/common/skill.md")],
                "cli": {
                    "claude-code": [
                        reference("development/specifications/cli/claude-code/skill.md")
                    ],
                    "codex": [reference("development/specifications/cli/codex/skill.md")],
                },
            },
            "design_rules": [reference("development/design-rules/skill.md")],
            "capability_profiles": [common_profile_ref, claude_profile_ref],
            "created_at": TIMESTAMP,
            "pal_format_version": "1.0",
        },
        "unit.schema.json": {
            "schema_version": 1,
            "unit_id": "unit-v1",
            "kind": "skill",
            "current_revision_id": None,
            "revision_ids": [],
            "created_at": TIMESTAMP,
            "updated_at": TIMESTAMP,
        },
        "development-revision.schema.json": {
            "schema_version": 1,
            "revision_id": "revision-v1",
            "creation_id": "creation-v1",
            "unit_id": "unit-v1",
            "request": {"summary": "Create a test Skill", "sha256": DIGEST},
            "source_files": [reference("source/request.md")],
            "artifacts": [
                {
                    "artifact_id": "artifact-v1",
                    "manifest": reference("artifacts/artifact-v1/artifact.json"),
                }
            ],
            "created_by_cli": "codex",
            "target_clis": TARGET_CLIS.copy(),
            "specification_refs": [reference("specifications/common.md")],
            "profile_refs": [reference("profiles/basic.json")],
            "created_at": TIMESTAMP,
        },
        "artifact.schema.json": {
            "schema_version": 1,
            "artifact_id": "artifact-v1",
            "unit_id": "unit-v1",
            "revision_id": "revision-v1",
            "kind": "skill",
            "profile_id": "skill-md-v1-basic",
            "covered_clis": TARGET_CLIS.copy(),
            "payload_root": "payload",
            "files": [file_entry],
            "tree_sha256": DIGEST,
            "dependencies": [],
        },
        "release.schema.json": {
            "schema_version": 1,
            "release_id": "release-v1",
            "unit_id": "unit-v1",
            "source_revision": {
                "revision_id": "revision-v1",
                "manifest": reference("development/revision.json"),
            },
            "artifacts": [artifact_snapshot],
            "payload_root": "payload",
            "files": [file_entry],
            "tree_sha256": DIGEST,
            "dependencies": [reference("payload/scripts/helper.py")],
            "created_at": TIMESTAMP,
            "pal_version": "0.1.0",
        },
        "production-version.schema.json": {
            "schema_version": 1,
            "production_version_id": "production-v1",
            "target_clis": TARGET_CLIS.copy(),
            "releases": [
                {
                    "release_id": "release-v1",
                    "unit_id": "unit-v1",
                    "manifest": reference("releases/unit-v1/release-v1/release.json"),
                }
            ],
            "artifacts": [production_artifact],
            "payload_root": "payload",
            "files": [file_entry],
            "tree_sha256": DIGEST,
            "composed_at": TIMESTAMP,
            "pal_version": "0.1.0",
        },
        "active-production.schema.json": {
            "schema_version": 1,
            "library_id": "library-v1",
            "active_production_version_id": None,
            "production_manifest": None,
            "production_mount_bundle": None,
            "activated_at": None,
        },
        "config-mount.schema.json": {
            "schema_version": 1,
            "mount_kind": "configuration",
            "mount_id": "config-library-v1-codex",
            "library_id": "library-v1",
            "cli_id": "codex",
            "library_root": "/tmp/pal/library",
            "library_manifest_sha256": DIGEST,
            "development_root": "/tmp/pal/library/development",
            "production_root": "/tmp/pal/library/production",
            "target_clis": TARGET_CLIS.copy(),
            "references": {
                "common_specifications": [absolute_reference("/tmp/pal/common.md")],
                "cli_specifications": [absolute_reference("/tmp/pal/codex.md")],
                "design_rules": [absolute_reference("/tmp/pal/rules.md")],
            },
            "profile_ids": ["skill-md-v1-basic"],
            "pal_version": "0.1.0",
            "adapter_version": "0.1.0",
            "verified_at": TIMESTAMP,
        },
        "production-mount.schema.json": {
            "schema_version": 1,
            "mount_kind": "production",
            "record_kind": "bundle",
            "mount_id": "production-library-v1",
            "library_id": "library-v1",
            "production_version_id": "production-v1",
            "production_manifest_sha256": DIGEST,
            "cli_records": [
                {
                    "cli_id": "claude-code",
                    "path": "claude-code.json",
                    "sha256": DIGEST,
                },
                {"cli_id": "codex", "path": "codex.json", "sha256": OTHER_DIGEST},
            ],
            "pal_version": "0.1.0",
            "verified_at": TIMESTAMP,
        },
        "production-transition.schema.json": {
            "schema_version": 1,
            "transaction_id": "transition-v1",
            "library_id": "library-v1",
            "from_version_id": None,
            "to_version_id": "production-v1",
            "phase": "PREPARING_TARGET",
            "old_active_sha256": DIGEST,
            "new_active_sha256": OTHER_DIGEST,
            "clis": {
                "claude-code": {
                    "old_projection": None,
                    "target_projection": projection(marketplace_id=None),
                    "install_status": "offline-verified",
                },
                "codex": {
                    "old_projection": None,
                    "target_projection": projection(),
                    "install_status": "unchanged",
                },
            },
            "recovery_action": "restore-old",
            "created_at": TIMESTAMP,
            "updated_at": TIMESTAMP,
            "process": {"pid": 100, "hostname": "test-host"},
        },
        "creation-transaction.schema.json": {
            "schema_version": 1,
            "creation_id": "creation-v1",
            "library_id": "library-v1",
            "created_by_cli": "claude-code",
            "state": "OPEN",
            "staging_root": "/tmp/pal/creation-v1",
            "request": reference("request/input.md"),
            "target_clis": TARGET_CLIS.copy(),
            "profile_ids": ["skill-md-v1-basic"],
            "created_at": TIMESTAMP,
            "updated_at": TIMESTAMP,
            "error": None,
        },
        "usage-transaction.schema.json": {
            "schema_version": 1,
            "usage_id": "usage-v1",
            "library_id": "library-v1",
            "cli_id": "codex",
            "state": "OPEN",
            "task": reference("task/input.txt"),
            "production_version_id": "production-v1",
            "production_manifest_sha256": DIGEST,
            "event_stream": reference("events/usage-v1.jsonl"),
            "selection": None,
            "started_at": TIMESTAMP,
            "updated_at": TIMESTAMP,
        },
        "usage-record.schema.json": {
            "schema_version": 1,
            "usage_id": "usage-v1",
            "task": {
                "task_id": "task-v1",
                "input": "Use the production Skill",
                "input_sha256": DIGEST,
            },
            "overall_result": {
                "status": "succeeded",
                "output": "Task completed",
                "output_sha256": OTHER_DIGEST,
                "process_exit_code": 0,
                "error_code": None,
            },
            "cli": {"id": "codex", "version": "0.147.0", "session_id": "session-v1"},
            "production_version_id": "production-v1",
            "logical_units": [{"unit_id": "unit-v1", "release_id": "release-v1"}],
            "loaded_artifacts": [
                {
                    "artifact_id": "artifact-v1",
                    "profile_id": "skill-md-v1-basic",
                    "covered_clis": TARGET_CLIS.copy(),
                    "sha256": DIGEST,
                }
            ],
            "evidence": {
                "selection": {
                    "kind": "actual-skill-load",
                    "event_index": 1,
                    "loaded_relative_path": "skills/test/SKILL.md",
                    "loaded_sha256": DIGEST,
                },
                "completion": {
                    "kind": "terminal-cli-event",
                    "result_code": "completed",
                    "process_exit_code": 0,
                    "signal": None,
                    "output_sha256": OTHER_DIGEST,
                },
                "capture": {
                    "kind": "controlled-jsonl-runner",
                    "event_stream_sha256": DIGEST,
                    "event_count": 3,
                    "mount_bundle_sha256": OTHER_DIGEST,
                    "production_manifest_sha256": DIGEST,
                    "recorder_version": "0.1.0",
                },
            },
            "started_at": TIMESTAMP,
            "completed_at": TIMESTAMP,
        },
        "capability-profile.schema.json": {
            "schema_version": 1,
            "profile_id": "skill-md-v1-basic",
            "artifact_kind": "skill",
            "extends": None,
            "covered_clis": TARGET_CLIS.copy(),
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
            "captured_at": TIMESTAMP,
        },
    }
    return deepcopy(samples)
