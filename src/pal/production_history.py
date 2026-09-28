"""Legacy marker path retained solely for cleanup recovery.

Traceability: PSM-008; PRD-RELEASE-002; ACC-008, ACC-012.
"""

from pathlib import Path

from .paths import require_inside, require_safe_id


def history_marker_path(config: Path, library_id: str, version_id: str) -> Path:
    require_safe_id(library_id, "library_id")
    require_safe_id(version_id, "production_version_id")
    return require_inside(
        config, f"history/{library_id}/{version_id}.json", "快照回收标记", must_exist=False
    )
