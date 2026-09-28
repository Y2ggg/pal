"""Explicit current-content diagnostics, separate from lightweight context checks.

Traceability: PRD-P0-002; ACC-011/012; WEB-004/006.
"""

from pathlib import Path

from .errors import IntegrityError, PALError
from .library import doctor_library, load_json_object
from .maintenance import maintenance_lock
from .paths import canonical_existing_root, require_inside, require_safe_id
from .publishing import validate_development_revision, validate_production_version
from .schema_catalog import validate_instance


def _development(root: Path) -> int:
    units = require_inside(root, "development/units", "开发库")
    count = 0
    for directory in sorted(units.iterdir()):
        if directory.is_symlink() or not directory.is_dir():
            raise IntegrityError(f"Skill 目录无效：{directory}")
        require_safe_id(directory.name, "Skill ID")
        unit = load_json_object(directory / "unit.json")
        validate_instance("unit.schema.json", unit)
        if unit["unit_id"] != directory.name:
            raise IntegrityError(f"Skill 目录与标识不一致：{directory}")
        validate_development_revision(
            root, directory.name, unit["current_revision_id"], require_writable_development=False
        )
        count += 1
    return count


def check_library_contents(path: Path) -> dict:
    """Validate each current layer; fail closed with its label, without publishing.

    The exclusive maintenance lock excludes all lifecycle writers for a coherent
    read. Installed CLI copies are checked separately by installation_monitor.
    """
    root = canonical_existing_root(path)
    with maintenance_lock(root, exclusive=True):
        result = doctor_library(root, require_writable_development=False)
        checks = []
        errors = []
        for layer, label, version in (
            ("development", "开发库", None),
            ("production", "生产库", result["published_production_version_id"]),
            ("mounted", "CLI 挂载库", result["active_production_version_id"]),
        ):
            try:
                count = (
                    _development(root)
                    if layer == "development"
                    else len(validate_production_version(root, version)["releases"])
                    if version is not None
                    else 0
                )
                checks.append(
                    {
                        "layer": layer,
                        "label": label,
                        "units": count,
                        "state": "healthy" if count else "empty",
                    }
                )
            except (PALError, OSError) as exc:
                errors.append(f"{label}异常：{exc}")
        if errors:
            passed = "；".join(f"{item['label']}通过（{item['units']} 项）" for item in checks)
            raise IntegrityError(
                "完整性检查未通过。" + "；".join(errors + ([passed] if passed else []))
            )
        return {**result, "content_checks": checks}
