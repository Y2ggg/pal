"""One-time 1.0 to 2.0 library migration with a full backup.

Traceability: PRD-P0-001, PRD-CREATE-001 through PRD-CREATE-005,
PRD-RELEASE-001/002, PRD-MOUNT-003; ACC-001, ACC-003, ACC-007,
ACC-008, ACC-012. The migration preserves old v1 records byte-for-byte,
adds the v2 schema catalog and DSH specification, rebuilds every mounted
Skill as a three-target shared revision, and leaves old production versions
readable as history.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import PAL_FORMAT_VERSION, SCHEMA_VERSION
from . import production_mount as mounts
from .adapters import install_creation_adapter
from .config_mount import (
    bind_default_creation_library,
    mount_config,
    resolve_config_root,
)
from .creation import begin_creation, commit_creation
from .errors import IntegrityError, PALError, ProductionError
from .io import atomic_replace_json, formatted_json_bytes, tree_digest
from .library import (
    DSH_SPEC_PATH,
    SHARED_PROFILE_PATH,
    _reference,
    _shared_profile,
    _write_schemas,
    doctor_library,
    load_json_object,
)
from .paths import canonical_existing_root, require_inside
from .publishing import (
    compose_production,
    create_release,
    library_lock,
    validate_production_version,
)
from .schema_catalog import TARGET_CLIS, validate_instance
from .stages import set_current_production
from .targets.dsh import dsh_installed, resolve_dsh_home

MIGRATION_VERSION = "2.0"


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _backup_path(library_root: Path, backup_root: Path | None) -> Path:
    if backup_root is not None:
        target = Path(backup_root).resolve()
    else:
        target = (
            Path.home()
            / "Library"
            / "Application Support"
            / "PAL Migration Backups"
            / f"1.0-to-2.0-{_timestamp()}"
        )
    if target.exists():
        raise ProductionError(f"migration backup path already exists: {target}")
    target.mkdir(parents=True)
    shutil.copytree(library_root, target / "library")
    return target


def _backup_config(config_root: Path, target: Path) -> None:
    if config_root.exists():
        shutil.copytree(config_root, target / "config")


def _restore(library_root: Path, config_root: Path, backup: Path) -> None:
    library_backup = backup / "library"
    config_backup = backup / "config"
    if library_root.exists():
        shutil.rmtree(library_root)
    shutil.copytree(library_backup, library_root)
    if config_backup.exists():
        if config_root.exists():
            shutil.rmtree(config_root)
        shutil.copytree(config_backup, config_root)


def _write_text(path: Path, material: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(material)


def _upgrade_manifest(root: Path) -> dict[str, Any]:
    manifest_path = require_inside(root, "library.json", "library manifest")
    manifest = load_json_object(manifest_path)
    if manifest.get("schema_version") != 1 or manifest.get("pal_format_version") != "1.0":
        raise ProductionError("library is not a 1.0 library; migration is not applicable")
    if manifest.get("target_clis") != ["claude-code", "codex"]:
        raise ProductionError("1.0 library target set is not the expected two targets")

    (root / "schemas" / "v2").mkdir(parents=True, exist_ok=True)
    (root / "development" / "specifications" / "cli" / "dsh").mkdir(parents=True, exist_ok=True)
    _write_schemas(root)

    dsh_spec = root / DSH_SPEC_PATH
    if not dsh_spec.exists():
        from .library import DSH_SPEC

        _write_text(dsh_spec, DSH_SPEC.encode("utf-8"))
    shared_path = root / SHARED_PROFILE_PATH
    shared = _shared_profile()
    if not shared_path.exists():
        _write_text(
            shared_path,
            formatted_json_bytes(shared),
        )

    old_profiles = {item["profile_id"]: item for item in manifest["capability_profiles"]}
    basic_ref = old_profiles["skill-md-v1-basic"]
    claude_ref = old_profiles["skill-md-v1-claude-model-pinned"]
    specifications = manifest["specifications"]
    specifications["cli"]["dsh"] = [_reference(dsh_spec, root)]
    upgraded = {
        "schema_version": SCHEMA_VERSION,
        "library_id": manifest["library_id"],
        "development_root": manifest["development_root"],
        "release_root": manifest["release_root"],
        "production_root": manifest["production_root"],
        "usage_record_root": manifest["usage_record_root"],
        "schemas_root": "schemas/v2",
        "schema_registry": _reference(root / "schemas/v2/index.json", root),
        "target_clis": list(TARGET_CLIS),
        "specifications": specifications,
        "design_rules": manifest["design_rules"],
        "capability_profiles": [
            basic_ref,
            claude_ref,
            _reference(shared_path, root)
            | {
                "profile_id": shared["profile_id"],
                "covered_clis": shared["covered_clis"],
                "cli_versions": shared["cli_versions"],
            },
        ],
        "created_at": manifest["created_at"],
        "pal_format_version": PAL_FORMAT_VERSION,
    }
    validate_instance("library.schema.json", upgraded)
    atomic_replace_json(manifest_path, upgraded)
    return upgraded


def _refresh_config(root: Path, config_root: Path) -> None:
    library_id = load_json_object(root / "library.json")["library_id"]
    mounts_root = config_root / "mounts" / "config" / library_id
    if mounts_root.is_dir():
        for entry in mounts_root.iterdir():
            if entry.is_file():
                entry.unlink()
    for cli_id in TARGET_CLIS:
        mount_config(root, cli_id, config_root=config_root)
    bind_default_creation_library(root, config_root=config_root)


def _unit_payload(payload: dict[str, bytes], unit_id: str) -> dict[str, bytes]:
    prefix = f"skills/{unit_id}/"
    selected = {}
    for relative, material in payload.items():
        if not relative.startswith(prefix):
            continue
        child = relative[len(prefix) :]
        if not child:
            raise IntegrityError(f"production payload has no Skill files for {unit_id}")
        selected[child] = material
    if not selected:
        raise ProductionError(f"production payload does not contain {unit_id}")
    return selected


def _rebuild_units(
    root: Path,
    config_root: Path,
    validated: dict[str, Any],
) -> str:
    payload = mounts._selected_payload_files(validated, "claude-code")
    release_ids: list[str] = []
    for release in sorted(validated["releases"], key=lambda item: item["unit_id"]):
        unit_id = release["unit_id"]
        base_revision = release["release"]["source_revision"]["revision_id"]
        request = {
            "schema_version": 2,
            "unit_id": unit_id,
            "summary": f"Migrate {unit_id} to the three-target shared profile",
            "base_revision_id": base_revision,
            "profile_by_cli": {cli_id: "skill-md-v1-shared" for cli_id in TARGET_CLIS},
        }
        with tempfile.TemporaryDirectory(prefix="pal-migrate-request-") as temporary:
            request_path = Path(temporary) / "request.json"
            request_path.write_bytes(formatted_json_bytes(request))
            opened = begin_creation(root, "dsh", request_path, config_root=config_root)
            candidates = opened["candidates"]
            if len(candidates) != 1:
                raise ProductionError(f"migration expected one shared candidate for {unit_id}")
            candidate = candidates[0]
            if candidate["profile_id"] != "skill-md-v1-shared":
                raise ProductionError(f"migration candidate profile is not shared for {unit_id}")
            skill_root = Path(candidate["skill_root"])
            shutil.rmtree(skill_root)
            skill_root.mkdir(parents=True)
            for relative, material in _unit_payload(payload, unit_id).items():
                target = skill_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(material)
            committed = commit_creation(root, opened["creation_id"], config_root=config_root)
        released = create_release(root, unit_id, committed["revision_id"])
        release_ids.append(released["release_id"])
    if not release_ids:
        raise ProductionError("migration found no mounted production Skills")
    composed = compose_production(root, release_ids)
    return composed["production_version_id"]


def migrate_library_v2(
    library_root: Path,
    *,
    config_root: Path | None = None,
    backup_root: Path | None = None,
    activate: bool = True,
) -> dict[str, Any]:
    """Upgrade one 1.0 library to 2.0; old records remain byte-identical."""
    root = canonical_existing_root(library_root)
    manifest = load_json_object(root / "library.json")
    if manifest.get("schema_version") != 1:
        raise ProductionError("library is already migrated or is not a 1.0 library")
    config = resolve_config_root(config_root, create=True)
    active_path = root / "production" / "active.json"
    active = load_json_object(active_path)
    version_id = active.get("active_production_version_id")
    if not isinstance(version_id, str):
        raise ProductionError("migration requires an active production version")
    if (config / "transitions" / "production" / f"{manifest['library_id']}.json").exists():
        raise ProductionError("存在未完成的 CLI 同步事务，请先恢复后再迁移")
    before = {
        "library": tree_digest(root),
        "config": tree_digest(config) if config.exists() else None,
    }
    backup = _backup_path(root, backup_root)
    _backup_config(config, backup)
    try:
        with library_lock(root, "migration", "library format migration"):
            _upgrade_manifest(root)
            doctor_library(root)
            _refresh_config(root, config)
            validated = validate_production_version(root, version_id)
            new_version = _rebuild_units(root, config, validated)
            set_current_production(root, new_version)
            activation = None
            if activate:
                from .production_mount import activate_production

                activation = activate_production(root, new_version, config_root=config)
                if dsh_installed(resolve_dsh_home()):
                    install_creation_adapter(root, "dsh", config_root=config)
                bind_default_creation_library(root, config_root=config)
            doctor = doctor_library(root)
        if activate:
            dsh_home = resolve_dsh_home()
            dsh_ok = (
                dsh_installed(dsh_home)
                and (dsh_home / "skills" / "pal-create-skill" / "SKILL.md").is_file()
            )
        else:
            dsh_home = None
            dsh_ok = False
        return {
            "proof": "PAL_LIBRARY_MIGRATED",
            "library_root": str(root),
            "config_root": str(config),
            "backup_root": str(backup),
            "from_format": "1.0",
            "to_format": MIGRATION_VERSION,
            "active_production_version_id": doctor["active_production_version_id"],
            "new_production_version_id": new_version,
            "dsh_home": str(dsh_home) if dsh_home is not None else None,
            "dsh_creation_entry_installed": dsh_ok,
            "activation": activation,
            "before_digest": before,
            "after_library_digest": tree_digest(root),
        }
    except (PALError, OSError, ValueError) as exc:
        _restore(root, config, backup)
        raise ProductionError(f"库格式迁移失败，已恢复备份：{exc}") from exc


__all__ = ["MIGRATION_VERSION", "migrate_library_v2"]
