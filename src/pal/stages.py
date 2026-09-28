"""Current published content, independently of installed CLI content.

Traceability: STG-001/002/006; PRD-P0-002; ACC-007, ACC-011, ACC-012.
"""

from pathlib import Path

from .errors import IntegrityError
from .io import atomic_replace_json, sha256_file
from .library import load_json_object
from .paths import require_inside
from .schema_catalog import validate_config_instance, validate_instance


def current_production_id(root: Path) -> str | None:
    """Read the publication pointer; legacy libraries use their installed version."""
    path = require_inside(root, "production/current.json", "当前生产", must_exist=False)
    library_id = load_json_object(root / "library.json")["library_id"]
    if not path.exists():
        active = load_json_object(require_inside(root, "production/active.json", "CLI 挂载状态"))
        validate_instance("active-production.schema.json", active)
        if active["library_id"] != library_id:
            raise IntegrityError("CLI 挂载状态与外挂库不匹配")
        return active["active_production_version_id"]
    current = load_json_object(path)
    validate_config_instance("current-production.schema.json", current)
    if current["library_id"] != library_id:
        raise IntegrityError("当前生产指针与外挂库不匹配")
    version = current["production_version_id"]
    manifest = require_inside(root, f"production/versions/{version}/production.json", "当前生产")
    if sha256_file(manifest) != current["manifest_sha256"]:
        raise IntegrityError("当前生产内容与发布指针不一致")
    return version


def set_current_production(root: Path, version_id: str) -> bool:
    """Called under the publication lock after composing a validated release set."""
    from .publishing import validate_production_version

    validated = validate_production_version(root, version_id)
    library_id = load_json_object(root / "library.json")["library_id"]
    changed = current_production_id(root) != version_id
    pointer = {
        "schema_version": 1,
        "library_id": library_id,
        "production_version_id": version_id,
        "manifest_sha256": validated["manifest_sha256"],
    }
    validate_config_instance("current-production.schema.json", pointer)
    path = require_inside(root, "production/current.json", "当前生产", must_exist=False)
    if not path.exists() or load_json_object(path) != pointer:
        atomic_replace_json(path, pointer)
    return changed
