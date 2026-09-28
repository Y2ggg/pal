"""Shared three-layer query for CLI and Web (STG-001/004; SLC-004/014; ACC-012)."""

from pathlib import Path
from typing import Any

from .config_mount import resolve_default_creation_library
from .errors import PathSafetyError
from .library import doctor_library, load_json_object
from .maintenance import lifecycle_write, skill_action_path
from .paths import require_inside, require_safe_id
from .production_mount import _transition_path
from .publishing import validate_development_revision, validate_production_version
from .schema_catalog import validate_instance
from .skill_browser import skill_artifacts
from .skill_state import needs_sync, read_skill_state
from .stages import current_production_id


def _json_file(path: Path) -> dict[str, Any]:
    value = load_json_object(path)
    if not isinstance(
        value, dict
    ):  # load_json_object currently guarantees this; keep the boundary explicit.
        raise PathSafetyError(f"PAL object is not a JSON object: {path}")
    return value


def _release_members(root: Path, version: str | None) -> dict[str, dict[str, Any]]:
    if version is None:
        return {}
    validated = validate_production_version(root, version)
    return {
        item["unit_id"]: {
            "release_id": item["release_id"],
            "revision_id": item["release"]["source_revision"]["revision_id"],
            "artifacts": skill_artifacts(item),
        }
        for item in validated["releases"]
    }


@lifecycle_write
def library_status(root: Path, config_root: Path) -> dict[str, Any]:
    doctor = doctor_library(root)
    default_library = resolve_default_creation_library(config_root=config_root)
    default_binding = {
        "configured": default_library is not None,
        "library_root": str(default_library) if default_library is not None else None,
        "matches_selected": default_library == root if default_library is not None else False,
    }

    active_version_id = current_production_id(root)
    mounted_version_id = doctor["active_production_version_id"]
    active_releases = _release_members(root, active_version_id)
    mounted_releases = _release_members(root, mounted_version_id)
    units_root = require_inside(root, "development/units", "development units root")
    development = {}
    for candidate in sorted(units_root.iterdir()):
        if candidate.is_symlink() or not candidate.is_dir():
            raise PathSafetyError(f"development unit entry is invalid: {candidate}")
        require_safe_id(candidate.name, "development unit directory")
        unit = _json_file(candidate / "unit.json")
        validate_instance("unit.schema.json", unit)
        if unit["unit_id"] != candidate.name:
            raise PathSafetyError(f"development unit identity differs: {candidate}")
        development[candidate.name] = unit
    selection = read_skill_state(root)
    disabled = set(selection["disabled_unit_ids"])
    pending_deletion = set(selection["pending_deletion_unit_ids"])
    sync_required = needs_sync(active_releases, mounted_releases, selection)
    units = []
    for unit_id in sorted(
        set(development) | set(active_releases) | set(mounted_releases) | pending_deletion
    ):
        unit = development.get(unit_id)
        current_revision = unit["current_revision_id"] if unit else None
        active = active_releases.get(unit_id)
        mounted = mounted_releases.get(unit_id)
        if active:
            artifacts, source = active["artifacts"], "production"
        elif unit:
            artifacts = skill_artifacts(
                validate_development_revision(root, unit_id, current_revision)
            )
            source = "development"
        else:
            artifacts, source = mounted["artifacts"] if mounted else [], "mounted"
        state = (
            "published"
            if active and active["revision_id"] == current_revision
            else "changed"
            if active and unit
            else "production-only"
            if active
            else "unpublished"
            if unit
            else "mounted-only"
        )
        units.append(
            {
                "unit_id": unit_id,
                "component_type": unit["kind"] if unit else "skill",
                "current_revision_id": current_revision,
                "development_revision_id": current_revision,
                "updated_at": unit["updated_at"] if unit else None,
                "active_release_id": active["release_id"] if active else None,
                "active_revision_id": active["revision_id"] if active else None,
                "production_revision_id": active["revision_id"] if active else None,
                "mounted_revision_id": mounted["revision_id"] if mounted else None,
                "state": state,
                "mount_enabled": unit_id not in disabled,
                "pending_deletion": unit_id in pending_deletion,
                "description": " / ".join(dict.fromkeys(item["description"] for item in artifacts)),
                "description_source": source,
            }
        )
    recovery_required = (
        _transition_path(config_root, doctor["library_id"]).exists()
        or skill_action_path(root).exists()
    )

    return {
        "library": {
            "library_id": doctor["library_id"],
            "library_root": doctor["library_root"],
            "phase": doctor["phase"],
            "library_manifest_sha256": doctor["library_manifest_sha256"],
            "target_clis": doctor["target_clis"],
        },
        "config_root": str(config_root),
        "default_binding": default_binding,
        "production": {
            "active_version_id": active_version_id,
            "members": [
                {"unit_id": unit, "revision_id": value["revision_id"]}
                for unit, value in active_releases.items()
            ],
        },
        "mount": {
            "version_id": mounted_version_id,
            "sync_required": sync_required,
            "recovery_required": recovery_required,
            "targets": [
                {
                    "cli_id": cli,
                    "version_id": mounted_version_id,
                    "state": "recovery-required"
                    if recovery_required
                    else "pending"
                    if sync_required
                    else "unknown",
                }
                for cli in doctor["target_clis"]
            ],
        },
        "units": units,
    }
