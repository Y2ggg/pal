"""Reviewed Skill operations with explicit publication and installation boundaries.

Traceability: SLC-001 through SLC-007; PRD-P0-002, PRD-CREATE-005;
PRD-MOUNT-003/004; ACC-008/011/012.
"""

from pathlib import Path

from . import deletion
from .config_mount import resolve_config_root
from .errors import IntegrityError, ProductionError
from .io import (
    atomic_replace_json,
    formatted_json_bytes,
    fsync_directory,
    sha256_bytes,
    sha256_file,
    tree_digest,
)
from .library import doctor_library, load_json_object, utc_now
from .maintenance import cleanup_path, maintenance_lock, skill_action_path
from .paths import canonical_existing_root, require_inside, require_safe_id
from .publication import _require_no_sync_transition, _sync_selected_locked, publish_unit
from .publishing import compose_production, library_lock, validate_development_revision
from .schema_catalog import validate_config_instance, validate_instance
from .skill_state import mounted_version, read_skill_state, release_members, write_skill_state
from .stages import current_production_id, set_current_production

ACTIONS = ("publish", "discard", "mount", "unmount", "delete")


def _no_open_creation(root: Path, unit_id: str) -> None:
    for candidate in deletion._children(root, ".pal/transactions/creation"):
        record = load_json_object(candidate / "transaction.json")
        validate_instance("creation-transaction.schema.json", record)
        if (
            record["state"] not in {"COMMITTED", "ABORTED"}
            and deletion._creation_request(candidate, record)["unit_id"] == unit_id
        ):
            raise IntegrityError(
                f"Skill 存在未完成创建或更新事务，请先完成或终止：{candidate.name}"
            )


def _prepare(root: Path, config: Path, action: str, unit_id: str) -> dict:
    if action not in ACTIONS:
        raise ValueError("不支持的 Skill 操作")
    require_safe_id(unit_id, "Skill ID")
    doctor = doctor_library(root)
    _require_no_sync_transition(root, config)
    state = read_skill_state(root)
    version = current_production_id(root)
    active = mounted_version(root)
    published = release_members(root, version).get(unit_id)
    installed = release_members(root, active).get(unit_id)
    unit_root = require_inside(
        root, f"development/units/{unit_id}", "Skill 开发内容", must_exist=False
    )
    unit = load_json_object(unit_root / "unit.json") if unit_root.exists() else None
    if unit is not None:
        validate_instance("unit.schema.json", unit)
        if unit["unit_id"] != unit_id:
            raise IntegrityError("Skill 目录与标识不一致")
        validate_development_revision(root, unit_id, unit["current_revision_id"])
    pending = unit_id in state["pending_deletion_unit_ids"]
    if not any([unit, published, installed, pending]):
        raise IntegrityError("找不到此 Skill，请刷新页面")
    if pending and action != "unmount":
        raise IntegrityError("此 Skill 正在删除，请先完成 CLI 卸载")
    if action == "publish" and unit is None:
        raise ProductionError("没有开发内容可发布")
    if action in {"mount", "discard"} and published is None:
        raise ProductionError("此 Skill 没有生产内容，请先发布")
    if action == "discard":
        if unit is None:
            raise ProductionError("没有未发布的开发修改")
        _no_open_creation(root, unit_id)
        source = published["release"]["source_revision"]
        restored = validate_development_revision(root, unit_id, source["revision_id"])
        if restored["revision_sha256"] != source["manifest"]["sha256"]:
            raise IntegrityError("生产对应的开发修订已漂移，拒绝恢复")
    if action == "delete":
        _no_open_creation(root, unit_id)
    plan = {
        "action": action,
        "unit_id": unit_id,
        "development_revision_id": unit["current_revision_id"] if unit else None,
        "production_revision_id": published["release"]["source_revision"]["revision_id"]
        if published
        else None,
        "mounted_revision_id": installed["release"]["source_revision"]["revision_id"]
        if installed
        else None,
        "production_version_id": version,
        "mounted_version_id": active,
        "target_clis": doctor["target_clis"],
        "pending_deletion": pending,
        "revision_count": len(unit["revision_ids"]) if unit else 0,
    }
    plan["token"] = sha256_bytes(
        formatted_json_bytes(
            [
                plan,
                str(root),
                str(config),
                state,
                tree_digest(unit_root) if unit else None,
                sha256_file(root / "production/active.json"),
            ]
        )
    )
    return plan


def preview_skill_action(
    library_root: Path, action: str, unit_id: str, *, config_root: Path | None = None
) -> dict:
    root = canonical_existing_root(library_root)
    config = resolve_config_root(config_root, create=False)
    with maintenance_lock(root, exclusive=True):
        return _prepare(root, config, action, unit_id)


def _finish_delete(root: Path, config: Path, journal: dict) -> dict:
    validate_config_instance("skill-action.schema.json", journal)
    library_id = load_json_object(root / "library.json")["library_id"]
    if (journal["library_id"], journal["library_root"], journal["config_root"]) != (
        library_id,
        str(root),
        str(config),
    ):
        raise IntegrityError("Skill 操作恢复记录与库或配置目录不一致")
    unit_id = journal["unit_id"]
    base = journal["base_production_version_id"]
    target = journal["production_version_id"]
    if current_production_id(root) not in {base, target}:
        raise IntegrityError("当前生产与已确认的删除范围不一致，拒绝覆盖")
    source_members = release_members(root, base)
    target_members = release_members(root, target)
    if {key: item["release_id"] for key, item in source_members.items() if key != unit_id} != {
        key: item["release_id"] for key, item in target_members.items()
    }:
        raise IntegrityError("删除目标必须仅移除此 Skill，不能改变其他生产内容")
    cleanup = journal["cleanup"]
    if cleanup is not None:
        validate_config_instance(deletion.JOURNAL_SCHEMA, cleanup)
        if (
            cleanup["library_id"],
            cleanup["library_root"],
            cleanup["config_root"],
            cleanup["plan"]["kind"],
            cleanup["plan"]["unit_id"],
        ) != (library_id, str(root), str(config), "skill", unit_id):
            raise IntegrityError("Skill 操作与开发清理范围不一致")
    _require_no_sync_transition(root, config)
    with library_lock(root, "publication", "删除 Skill"):
        target = journal["production_version_id"]
        if unit_id in release_members(root, target):
            raise IntegrityError("删除目标生产集合仍包含此 Skill")
        if target is not None:
            set_current_production(root, target)
        state = read_skill_state(root)
        pending = set(state["pending_deletion_unit_ids"])
        disabled = set(state["disabled_unit_ids"])
        if unit_id in release_members(root, mounted_version(root)):
            pending.add(unit_id)
            disabled.add(unit_id)
        else:
            pending.discard(unit_id)
            disabled.discard(unit_id)
        state["pending_deletion_unit_ids"] = sorted(pending)
        state["disabled_unit_ids"] = sorted(disabled)
        write_skill_state(root, state)
    cleanup = journal["cleanup"]
    if cleanup is not None:
        validate_config_instance(deletion.JOURNAL_SCHEMA, cleanup)
        if cleanup["plan"]["kind"] != "skill" or cleanup["plan"]["unit_id"] != unit_id:
            raise IntegrityError("Skill 操作与开发清理范围不一致")
        if cleanup_path(root).exists() and load_json_object(cleanup_path(root)) != cleanup:
            raise IntegrityError("存在其他清理任务，拒绝覆盖")
        atomic_replace_json(cleanup_path(root), cleanup)
        deletion.recover_deletion(root, config_root=config)
    skill_action_path(root).unlink()
    fsync_directory(root / ".pal")
    return {
        "deleted": unit_id not in pending,
        "pending_deletion": unit_id in pending,
        "unit_id": unit_id,
        "message": "开发与生产已移除；请完成 CLI 卸载，条目会保留到清理完成。"
        if unit_id in pending
        else "Skill 已退出管理，内部既有证据保留。",
    }


def execute_skill_action(
    library_root: Path, action: str, unit_id: str, token: str, *, config_root: Path | None = None
) -> dict:
    root = canonical_existing_root(library_root)
    config = resolve_config_root(config_root, create=False)
    with maintenance_lock(root, exclusive=True):
        plan = _prepare(root, config, action, unit_id)
        if token != plan["token"]:
            raise IntegrityError("Skill 内容或挂载状态已变化，请重新预览并确认")
        if action == "publish":
            return publish_unit(root, unit_id, config_root=config)
        if action in {"mount", "unmount"}:
            with library_lock(root, "publication", "Skill 挂载"):
                result = _sync_selected_locked(
                    root, config_root=config, unit_id=unit_id, mounted=action == "mount"
                )
            return {
                **result,
                "message": "已挂载当前生产内容，请在新 CLI 会话使用。"
                if action == "mount"
                else "CLI 卸载完成。"
                if plan["pending_deletion"]
                else "已从 CLI 卸载；Skill 与生产内容保留，可重新挂载。",
            }
        if action == "discard":
            with library_lock(root, f"unit-{unit_id}", "放弃未发布修改"):
                path = require_inside(root, f"development/units/{unit_id}/unit.json", "开发内容")
                unit = load_json_object(path)
                unit["current_revision_id"] = plan["production_revision_id"]
                unit["updated_at"] = utc_now()
                validate_instance("unit.schema.json", unit)
                atomic_replace_json(path, unit)
            return {
                "unit_id": unit_id,
                "revision_id": plan["production_revision_id"],
                "message": "开发内容已恢复到当前生产版；生产和 CLI 挂载保持不变。",
            }
        cleanup = None
        if plan["development_revision_id"] is not None:
            development_plan = deletion.preview_deletion(root, "skill", unit_id, config_root=config)
            cleanup = {
                "schema_version": 1,
                "library_root": str(root),
                "config_root": str(config),
                "library_id": load_json_object(root / "library.json")["library_id"],
                "plan": development_plan,
            }
        target = None
        if plan["production_version_id"] is not None:
            members = release_members(root, plan["production_version_id"])
            target = compose_production(
                root, [item["release_id"] for key, item in members.items() if key != unit_id]
            )["production_version_id"]
        journal = {
            "schema_version": 1,
            "library_root": str(root),
            "config_root": str(config),
            "library_id": load_json_object(root / "library.json")["library_id"],
            "unit_id": unit_id,
            "base_production_version_id": plan["production_version_id"],
            "production_version_id": target,
            "cleanup": cleanup,
        }
        validate_config_instance("skill-action.schema.json", journal)
        atomic_replace_json(skill_action_path(root), journal)
        return _finish_delete(root, config, journal)


def recover_skill_action(library_root: Path, *, config_root: Path | None = None) -> dict:
    root = canonical_existing_root(library_root)
    config = resolve_config_root(config_root, create=False)
    with maintenance_lock(root, exclusive=True, recovery=True):
        path = skill_action_path(root)
        if not path.exists():
            return {"recovered": False}
        return {"recovered": True, **_finish_delete(root, config, load_json_object(path))}
