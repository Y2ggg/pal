"""Mount intent and unfinished Skill removal, separate from published content.

Traceability: SLC-001/004/005, STG-002/004; ACC-008/011/012.
"""

from pathlib import Path

from .errors import IntegrityError
from .io import atomic_replace_json
from .library import load_json_object
from .paths import require_inside
from .schema_catalog import validate_config_instance


def needs_sync(published: dict, mounted: dict, selection: dict) -> bool:
    """Compare selected content, including unfinished removals (SLC-004/005/014)."""
    disabled = set(selection["disabled_unit_ids"])
    desired = {unit: item["release_id"] for unit, item in published.items() if unit not in disabled}
    actual = {unit: item["release_id"] for unit, item in mounted.items()}
    return desired != actual or bool(selection["pending_deletion_unit_ids"])


def read_skill_state(root: Path) -> dict:
    library_id = load_json_object(root / "library.json")["library_id"]
    path = require_inside(root, "production/skill-state.json", "Skill 挂载选择", must_exist=False)
    if not path.exists():
        return {
            "schema_version": 1,
            "library_id": library_id,
            "disabled_unit_ids": [],
            "pending_deletion_unit_ids": [],
        }
    state = load_json_object(path)
    validate_config_instance("skill-state.schema.json", state)
    if state["library_id"] != library_id:
        raise IntegrityError("Skill 挂载选择与外挂库不匹配")
    if not set(state["pending_deletion_unit_ids"]).issubset(state["disabled_unit_ids"]):
        raise IntegrityError("待删除 Skill 必须停止挂载")
    return state


def write_skill_state(root: Path, state: dict) -> None:
    """Caller holds the publication lock or exclusive maintenance lock."""
    validate_config_instance("skill-state.schema.json", state)
    if state["library_id"] != load_json_object(root / "library.json")["library_id"]:
        raise IntegrityError("Skill 挂载选择与外挂库不匹配")
    if not set(state["pending_deletion_unit_ids"]).issubset(state["disabled_unit_ids"]):
        raise IntegrityError("待删除 Skill 必须停止挂载")
    atomic_replace_json(
        require_inside(root, "production/skill-state.json", "Skill 挂载选择", must_exist=False),
        state,
    )


def require_live_skill(root: Path, unit_id: str) -> None:
    if unit_id in read_skill_state(root)["pending_deletion_unit_ids"]:
        raise IntegrityError("此 Skill 正在删除，请先完成 CLI 卸载后再创建或发布")


def release_members(root: Path, version: str | None) -> dict:
    from .publishing import validate_production_version

    if version is None:
        return {}
    return {
        item["unit_id"]: item for item in validate_production_version(root, version)["releases"]
    }


def mounted_version(root: Path) -> str | None:
    from .schema_catalog import validate_instance

    active = load_json_object(require_inside(root, "production/active.json", "CLI 挂载状态"))
    validate_instance("active-production.schema.json", active)
    if active["library_id"] != load_json_object(root / "library.json")["library_id"]:
        raise IntegrityError("CLI 挂载状态与外挂库不匹配")
    return active["active_production_version_id"]
