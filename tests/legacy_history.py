"""Legacy journal fixture builder, excluded from the installed product.

Only tests use these retired writers to exercise old evidence and recovery.
"""

from pal import deletion as d
from pal import production_mount as mounts
from pal.deletion import (
    _archive_path,
    _cache_parent,
    _cache_paths,
    _children,
    _claude_root,
    _creation_request,
    _orphan_marker,
    _paths,
    _protect_references,
)
from pal.errors import IntegrityError
from pal.io import (
    atomic_replace_json,
    formatted_json_bytes,
    sha256_bytes,
    sha256_file,
    tree_digest,
)
from pal.library import doctor_library, load_json_object, utc_now
from pal.paths import require_inside, require_safe_id
from pal.production_history import history_marker_path
from pal.publishing import validate_production_version, validate_release
from pal.schema_catalog import validate_config_instance, validate_instance


def _cache_inventory(validated, library_id, version):
    cache_root = _claude_root()
    if cache_root is None:
        return []
    parent = _cache_parent(cache_root, library_id, version)
    if not parent.exists():
        return []
    plugin, _, by_cli = mounts._projection_files(validated, library_id, version["version_id"])
    prefix = f"plugins/{plugin}/"
    expected = {
        path.removeprefix(prefix): content for path, content in by_cli["claude-code"].items()
    }
    result = []
    for candidate in sorted(parent.iterdir()):
        require_safe_id(candidate.name, "Claude 缓存版本")
        if candidate.is_symlink() or not candidate.is_dir():
            raise IntegrityError("PAL Claude 缓存版本目录异常")
        candidate_expected = dict(expected)
        marker = candidate / ".orphaned_at"
        if marker.exists():
            candidate_expected[".orphaned_at"] = _orphan_marker(marker)
        mounts._verify_files(candidate, candidate_expected, "待删除的 Claude 缓存")
        result.append(
            {
                "directory": candidate.name,
                "files": [
                    {"path": path, "sha256": sha256_bytes(content)}
                    for path, content in sorted(candidate_expected.items())
                ],
            }
        )
    return result


def _prepare(root, config, library_id, kind, object_id):
    if kind not in ("skill", "snapshot"):
        raise ValueError("删除类型必须是 skill 或 snapshot")
    require_safe_id(object_id, "删除对象 ID")
    doctor_library(root)
    plan = {
        "kind": kind,
        "object_id": object_id,
        "unit_id": object_id if kind == "skill" else None,
        "versions": [],
        "releases": [],
        "creation_ids": [],
        "revision_count": 0,
        "claude_cache_root": str(_claude_root()) if _claude_root() else None,
        "usage_references": [],
    }
    retained_releases = set()
    selected_releases = set()
    for candidate in _children(root, "production/versions") + _children(
        root, "records/usage/snapshots"
    ):
        validated = validate_production_version(root, candidate.name)
        refs = {(r["unit_id"], r["release_id"]) for r in validated["releases"]}
        unit_ids = sorted({unit for unit, _ in refs})
        selected = (
            kind == "snapshot"
            and candidate.name == object_id
            and candidate.parent.name == "versions"
        )
        if not selected:
            retained_releases.update(refs)
            continue
        selected_releases.update(refs)
        materialized = any(
            require_inside(config, relative, "快照投影", must_exist=False).exists()
            for relative in (
                f"projections/{library_id}/versions/{candidate.name}",
                f"mounts/production/{library_id}/versions/{candidate.name}",
                f"installs/claude-code/{library_id}/versions/{candidate.name}",
            )
        )
        plan["versions"].append(
            {
                "version_id": candidate.name,
                "unit_ids": unit_ids,
                "stable": validated["production"]["schema_version"] >= 2,
                "materialized": materialized,
                "cache_versions": [],
                "archive_required": False,
                "archive_path": None,
                "tree_sha256": tree_digest(candidate),
            }
        )
        plan["versions"][-1]["cache_versions"] = _cache_inventory(
            validated, library_id, plan["versions"][-1]
        )
        plan["versions"][-1]["archive_path"] = str(_archive_path(root, candidate.name))
    if kind == "snapshot" and not plan["versions"]:
        raise IntegrityError("该历史快照已不存在，请刷新页面")
    if kind == "skill":
        unit_path = require_inside(root, f"development/units/{object_id}/unit.json", "Skill")
        unit = load_json_object(unit_path)
        validate_instance("unit.schema.json", unit)
        if unit["unit_id"] != object_id:
            raise IntegrityError("Skill 目录与标识不一致")
        plan["revision_count"] = len(unit["revision_ids"])
        for candidate in _children(root, f"releases/units/{object_id}"):
            validate_release(root, object_id, candidate.name)
            selected_releases.add((object_id, candidate.name))
        for candidate in _children(root, ".pal/transactions/creation"):
            transaction = load_json_object(candidate / "transaction.json")
            validate_instance("creation-transaction.schema.json", transaction)
            if _creation_request(candidate, transaction)["unit_id"] == object_id:
                plan["creation_ids"].append(candidate.name)
    plan["releases"] = [
        {"unit_id": u, "release_id": r} for u, r in sorted(selected_releases - retained_releases)
    ]
    if kind == "snapshot":
        _protect_references(root, config, library_id, plan)
        for version in plan["versions"]:
            version["archive_required"] = bool(plan["usage_references"])
        if plan["usage_references"]:
            mounts.validate_production_mount(root, object_id, config_root=config)
            # The archived production manifest still points to these release
            # trees; retain them so usage validation remains complete.
            plan["releases"] = []
    else:
        # A Skill is a development object. Existing immutable snapshots are
        # independent and must never be selected or cascaded here.
        _protect_references(root, config, library_id, plan)
    inventories = []
    for path in _paths(root, config, library_id, plan) + _cache_paths(library_id, plan):
        digest = (
            tree_digest(path) if path.is_dir() else sha256_file(path) if path.exists() else None
        )
        inventories.append([str(path), digest])
    plan["token"] = sha256_bytes(
        formatted_json_bytes([plan, inventories, sha256_file(root / "production/active.json")])
    )
    return plan


def preview(library_root, kind, object_id, *, config_root=None):
    if kind == "skill":
        return d.preview_deletion(library_root, kind, object_id, config_root=config_root)
    with d._locked(library_root, config_root) as (root, config, library_id):
        return _prepare(root, config, library_id, kind, object_id)


def execute(library_root, kind, object_id, token, *, config_root=None):
    with d._locked(library_root, config_root) as (root, config, library_id):
        plan = (d._prepare if kind == "skill" else _prepare)(
            root, config, library_id, kind, object_id
        )
        if token != plan["token"]:
            raise IntegrityError("删除范围或内容已变化，请重新预览并确认")
        journal = {
            "schema_version": 1,
            "library_root": str(root),
            "config_root": str(config),
            "library_id": library_id,
            "plan": plan,
        }
        validate_config_instance(d.JOURNAL_SCHEMA, journal)
        atomic_replace_json(d.cleanup_path(root), journal)
        return d._finish(root, config, library_id, journal)


def seed_trash(library_root, version_id, *, deleted, config_root):
    library_id = load_json_object(library_root / "library.json")["library_id"]
    path = history_marker_path(config_root, library_id, version_id)
    if not deleted:
        path.unlink(missing_ok=True)
        return
    validated = validate_production_version(library_root, version_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace_json(
        path,
        {
            "schema_version": 1,
            "library_id": library_id,
            "library_root": str(library_root),
            "production_version_id": version_id,
            "manifest_sha256": validated["manifest_sha256"],
            "deleted_at": utc_now(),
        },
    )
