"""User-facing publication workflow over immutable release and production APIs.

Traceability: PRD-RELEASE-001, PRD-RELEASE-002, PRD-MOUNT-003,
PRD-MOUNT-004; ACC-007, ACC-008, ACC-011, ACC-012.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .components import SKILL_COMPONENT_TYPE_ID, component_type_driver
from .errors import IntegrityError, ProductionError
from .library import doctor_library, load_json_object
from .maintenance import lifecycle_write
from .paths import canonical_existing_root, require_inside, require_safe_id
from .platform_support import is_link
from .production_mount import activate_production
from .publishing import (
    compose_production,
    create_release,
    library_lock,
    validate_production_version,
)
from .schema_catalog import validate_instance
from .skill_state import (
    mounted_version,
    read_skill_state,
    release_members,
    require_live_skill,
    write_skill_state,
)
from .stages import current_production_id, set_current_production


def _require_no_sync_transition(root: Path, config_root: Path | None) -> None:
    from .config_mount import resolve_config_root
    from .production_mount import _transition_path

    library_id = load_json_object(root / "library.json")["library_id"]
    path = _transition_path(resolve_config_root(config_root, create=False), library_id)
    if path.exists() or is_link(path):
        raise ProductionError("存在未完成的 CLI 同步，请先执行异常恢复")


@lifecycle_write
def sync_production(
    library_root: Path,
    *,
    config_root: Path | None = None,
    expected_version_id: str | None = None,
) -> dict[str, Any]:
    """Explicitly install the current publication; never accept an arbitrary old version."""
    root = canonical_existing_root(library_root)
    with library_lock(root, "publication", "CLI 同步"):
        doctor_library(root)
        version = current_production_id(root)
        if version is None:
            raise ProductionError("尚无已发布内容，请先发布到生产")
        if expected_version_id is not None and expected_version_id != version:
            raise ProductionError("当前生产已变化，请刷新后重新确认同步")
        result = _sync_selected_locked(root, config_root=config_root)
        return {**result, "synced": True, "message": "同步完成，请在新 CLI 会话中使用。"}


def _sync_selected_locked(
    root: Path, *, config_root: Path | None, unit_id: str | None = None, mounted: bool | None = None
) -> dict:
    """Sync all enabled publications, or just one reviewed Skill; publication lock held."""
    _require_no_sync_transition(root, config_root)
    version = current_production_id(root)
    if version is None:
        raise ProductionError("尚无已发布内容，请先发布到生产")
    # Freeze the legacy publication before installing a selected subset.
    set_current_production(root, version)
    published = release_members(root, version)
    previous = release_members(root, mounted_version(root))
    state = read_skill_state(root)
    disabled = set(state["disabled_unit_ids"])
    pending = set(state["pending_deletion_unit_ids"])
    if unit_id is not None:
        desired = dict(previous)
        if mounted:
            require_live_skill(root, unit_id)
            if unit_id not in published:
                raise ProductionError("此 Skill 尚未发布，无法挂载")
            desired[unit_id] = published[unit_id]
            disabled.discard(unit_id)
        else:
            desired.pop(unit_id, None)
            disabled.add(unit_id)
        # Preserve explicit intent on failure. Retrying never silently reenables a Skill.
        state["disabled_unit_ids"] = sorted(disabled)
        write_skill_state(root, state)
    else:
        desired = {key: value for key, value in published.items() if key not in disabled}

    def identities(members):
        return {key: item["release_id"] for key, item in members.items()}

    if identities(desired) == identities(published):
        selected_version = version
    elif identities(desired) == identities(previous) and mounted_version(root) is not None:
        selected_version = mounted_version(root)
    else:
        selected_version = compose_production(
            root, [item["release_id"] for item in desired.values()]
        )["production_version_id"]
    result = activate_production(root, selected_version, config_root=config_root)
    # Activation verifies both CLI installations before completing pending removals.
    completed = (
        pending.difference(desired)
        if unit_id is None
        else pending.intersection({unit_id}).difference(desired)
    )
    if completed:
        state["pending_deletion_unit_ids"] = sorted(pending - completed)
        state["disabled_unit_ids"] = sorted(disabled - completed)
        write_skill_state(root, state)
    return {
        **result,
        "published_production_version_id": version,
        "completed_deletion_unit_ids": sorted(completed),
    }


def _current_unit(root: Path, unit_id: str) -> dict[str, str]:
    unit_root = require_inside(root, f"development/units/{unit_id}", "logical unit")
    if not unit_root.is_dir():
        raise IntegrityError(f"logical unit is not a directory: {unit_root}")
    unit = load_json_object(unit_root / "unit.json")
    validate_instance("unit.schema.json", unit)
    if unit["unit_id"] != unit_id:
        raise IntegrityError("logical unit identity mismatch")
    component_type_driver(unit["kind"])
    revision_id = require_safe_id(unit["current_revision_id"], "current revision ID")
    if revision_id not in unit["revision_ids"]:
        raise IntegrityError("current revision is absent from the logical unit history")
    return {
        "component_type": unit["kind"],
        "revision_id": revision_id,
    }


def _release_set(
    root: Path,
    *,
    active_version_id: str | None,
    unit_id: str,
    release_id: str,
) -> tuple[list[str], list[str]]:
    if active_version_id is None:
        return [release_id], []
    active = validate_production_version(root, active_version_id)
    preserved = [item for item in active["releases"] if item["unit_id"] != unit_id]
    return (
        [*[item["release_id"] for item in preserved], release_id],
        [item["unit_id"] for item in preserved],
    )


@lifecycle_write
def publish_unit(
    library_root: Path,
    unit_id: str,
    *,
    config_root: Path | None = None,
    sync: bool = False,
) -> dict[str, Any]:
    """Publish one unit; installation requires a separate explicit sync intent."""

    root = canonical_existing_root(library_root)
    unit_id = require_safe_id(unit_id, "unit_id")
    require_live_skill(root, unit_id)
    with library_lock(root, "publication", "library publication"):
        doctor = doctor_library(root)
        _require_no_sync_transition(root, config_root)
        previous = current_production_id(root)
        unit = _current_unit(root, unit_id)

        release = create_release(root, unit_id, unit["revision_id"])
        requested_releases, preserved_units = _release_set(
            root,
            active_version_id=previous,
            unit_id=unit_id,
            release_id=release["release_id"],
        )
        production = compose_production(root, requested_releases)
        set_current_production(root, production["production_version_id"])
        mount_enabled = unit_id not in read_skill_state(root)["disabled_unit_ids"]
        activation = (
            _sync_selected_locked(
                root,
                config_root=config_root,
                unit_id=unit_id,
                mounted=mount_enabled,
            )
            if sync
            else None
        )
        from .production_mount import _projection_identity

        plugin_name, _ = _projection_identity(
            doctor["library_id"], production["production_version_id"], stable=True
        )

        invocations: dict[str, str] = {}
        if unit["component_type"] == SKILL_COMPONENT_TYPE_ID:
            invocations = {
                "claude-code": f"{plugin_name}:{unit_id}",
                "codex": f"${plugin_name}:{unit_id}",
            }
        return {
            "library_id": doctor["library_id"],
            "unit_id": unit_id,
            "component_type": unit["component_type"],
            "revision_id": unit["revision_id"],
            "release_id": release["release_id"],
            "release_idempotent": release["idempotent"],
            "release_ids": production["release_ids"],
            "preserved_unit_ids": preserved_units,
            "production_version_id": production["production_version_id"],
            "production_idempotent": production["idempotent"],
            "previous_production_version_id": previous,
            "activation_idempotent": activation["idempotent"] if activation else None,
            "plugin_name": plugin_name,
            "effective_on": activation["effective_on"] if activation else "manual-sync",
            "synced": bool(activation),
            "mount_enabled": mount_enabled,
            "message": "已发布到生产；此 Skill 保持卸载，可在 Web 中手动重新挂载。"
            if not mount_enabled
            else "发布并同步完成，请在新 CLI 会话中使用。"
            if sync
            else "已发布到生产；CLI 挂载未改变，请按需手动同步。",
            "invocations": invocations,
        }


@lifecycle_write
def migrate_production_namespace(
    library_root: Path, *, config_root: Path | None = None, sync: bool = False
) -> dict[str, Any]:
    """Recompose only active releases using v2; never publish development (PSM-005)."""
    root = canonical_existing_root(library_root)
    with library_lock(root, "publication", "library publication"):
        doctor_library(root)
        _require_no_sync_transition(root, config_root)
        version = current_production_id(root)
        if version is None:
            raise ProductionError("尚无生产内容，无需迁移调用名")
        active = validate_production_version(root, version)
        production = compose_production(root, [item["release_id"] for item in active["releases"]])
        set_current_production(root, production["production_version_id"])
        if sync:
            return _sync_selected_locked(root, config_root=config_root)
        return {
            **production,
            "effective_on": "manual-sync",
            "message": "调用名已更新到生产，请手动同步 CLI。",
        }


__all__ = ["publish_unit", "sync_production"]
