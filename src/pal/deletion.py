"""Preview and durably resume permanent deletion of inactive PAL objects.

Traceability: DEL-001 through DEL-005; PRD-RELEASE-002; ACC-008, ACC-012.
"""

import os
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from . import production_mount as mounts
from .config_mount import resolve_config_root
from .errors import IntegrityError
from .io import (
    formatted_json_bytes,
    fsync_directory,
    sha256_bytes,
    sha256_file,
    tree_digest,
)
from .library import doctor_library, load_json_object
from .maintenance import cleanup_path, maintenance_lock
from .paths import canonical_existing_root, require_inside, require_safe_id, validate_regular_tree
from .production_history import history_marker_path
from .publishing import library_lock, validate_production_version, validate_release
from .schema_catalog import validate_config_instance, validate_instance
from .stages import current_production_id

JOURNAL_SCHEMA = "permanent-deletion.schema.json"


@contextmanager
def _locked(library_root, config_root, *, recovery=False):
    root = canonical_existing_root(library_root)
    config = resolve_config_root(config_root, create=True)
    with maintenance_lock(root, exclusive=True, recovery=recovery):
        library = load_json_object(root / "library.json")
        validate_instance("library.schema.json", library)
        library_id = library["library_id"]
        with (
            library_lock(root, "publication", "永久删除"),
            mounts._activation_lock(config, library_id),
        ):
            transition = mounts._transition_path(config, library_id)
            if transition.exists() or transition.is_symlink():
                raise IntegrityError("存在未完成的生产切换，请先执行异常恢复")
            yield root, config, library_id


def _children(root, relative):
    parent = require_inside(root, relative, "删除引用检查", must_exist=False)
    if not parent.exists():
        return []
    validate_regular_tree(parent)
    children = sorted(parent.iterdir())
    for child in children:
        require_safe_id(child.name, "对象目录")
        if not child.is_dir():
            raise IntegrityError(f"对象目录异常：{child}")
    return children


def _protect_references(root, config, library_id, plan):
    selected = {v["version_id"] for v in plan["versions"]}
    active, _ = mounts._read_active(root, library_id)
    if selected.intersection({active["active_production_version_id"], current_production_id(root)}):
        raise IntegrityError("当前生产或 CLI 挂载仍在使用此内容，不能清理")
    usage_references = []
    records = require_inside(root, "records/usage", "使用记录引用")
    validate_regular_tree(records)
    for path in sorted(records.glob("*/*/*.json")):
        if not (path.parent.parent.name.isdigit() and path.parent.name.isdigit()):
            continue
        record = load_json_object(path)
        if record.get("production_version_id") in selected:
            usage_references.append(path.relative_to(root).as_posix())
    pending = require_inside(root, ".pal/transactions/usage", "未完成使用事务")
    validate_regular_tree(pending)
    for path in sorted(pending.rglob("*.json")):
        if load_json_object(path).get("production_version_id") in selected:
            raise IntegrityError(f"存在未完成使用事务，请先执行异常恢复：{path.relative_to(root)}")
    plan["usage_references"] = sorted(set(usage_references))
    removed_releases = {(r["unit_id"], r["release_id"]) for r in plan["releases"]}
    for candidate in _children(root, "production/versions") + _children(
        root, "records/usage/snapshots"
    ):
        if candidate.name in selected:
            continue
        validated = validate_production_version(root, candidate.name)
        if any((r["unit_id"], r["release_id"]) in removed_releases for r in validated["releases"]):
            raise IntegrityError("仍有保留生产材料引用待删除的 release")
    if plan["unit_id"]:
        for candidate in _children(root, ".pal/transactions/creation"):
            if candidate.name in plan["creation_ids"] and cleanup_path(root).exists():
                continue  # This already-confirmed completed transaction may be partly erased.
            transaction = load_json_object(candidate / "transaction.json")
            validate_instance("creation-transaction.schema.json", transaction)
            request = _creation_request(candidate, transaction)
            if request["unit_id"] == plan["unit_id"] and transaction["state"] not in {
                "COMMITTED",
                "ABORTED",
            }:
                raise IntegrityError(
                    f"Skill 存在未完成的创建或更新事务，请先完成或终止：{candidate.name}"
                )


def _creation_request(candidate, transaction):
    path = require_inside(candidate, transaction["request"]["path"], "创建请求")
    if sha256_file(path) != transaction["request"]["sha256"]:
        raise IntegrityError("创建请求内容与事务记录不一致")
    return load_json_object(path)


def _claude_root():
    selected = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    return canonical_existing_root(selected) if selected.exists() else None


def _cache_parent(cache_root, library_id, version):
    plugin, marketplace = mounts._projection_identity(
        library_id, version["version_id"], stable=version["stable"]
    )
    return require_inside(
        cache_root, f"plugins/cache/{marketplace}/{plugin}", "PAL Claude 缓存", must_exist=False
    )


def _orphan_marker(path):
    if path.is_symlink() or not path.is_file():
        raise IntegrityError("Claude 缓存回收标记异常")
    raw = path.read_bytes()
    if re.fullmatch(rb"[0-9]{10,16}", raw) is None:
        raise IntegrityError("Claude 缓存回收标记必须是时间戳")
    return raw


def _cache_paths(library_id, plan):
    if not any(version["cache_versions"] for version in plan["versions"]):
        return []
    cache_root = _claude_root()
    if cache_root is None or str(cache_root) != plan["claude_cache_root"]:
        raise IntegrityError("Claude 缓存目录与删除预览不一致，请恢复原环境后重试")
    result = []
    for version in plan["versions"]:
        parent = _cache_parent(cache_root, library_id, version)
        for cache in version["cache_versions"]:
            path = require_inside(
                cache_root,
                (parent / cache["directory"]).relative_to(cache_root).as_posix(),
                "待清理缓存",
                must_exist=False,
            )
            if path.exists():
                validate_regular_tree(path)
                expected = {item["path"]: item["sha256"] for item in cache["files"]}
                # A retry may see a subset after a crash; changed/new files are never erased.
                for file in path.rglob("*"):
                    if file.relative_to(path).as_posix() == ".orphaned_at":
                        _orphan_marker(file)  # Official uninstall may add/update this marker.
                        continue
                    if file.is_file() and expected.get(
                        file.relative_to(path).as_posix()
                    ) != sha256_file(file):
                        raise IntegrityError("Claude 缓存发生变化，拒绝继续删除")
            result.append(path)
    return result


def _paths(root, config, library_id, plan):
    """Only fixed layouts, never filesystem paths supplied by the browser/journal."""
    paths = []
    for version in plan["versions"]:
        version_id = require_safe_id(version["version_id"], "production version ID")
        paths.append(
            require_inside(root, f"production/versions/{version_id}", "快照", must_exist=False)
        )
        relatives = [
            f"installs/claude-code/{library_id}/versions/{version_id}",
            f"installs/codex/{library_id}/versions/{version_id}",
        ]
        if not version["archive_required"]:
            relatives += [
                f"projections/{library_id}/versions/{version_id}",
                f"mounts/production/{library_id}/versions/{version_id}",
            ]
        for relative in relatives:
            paths.append(require_inside(config, relative, "快照挂载材料", must_exist=False))
        paths.append(history_marker_path(config, library_id, version_id))
    for release in plan["releases"]:
        unit = require_safe_id(release["unit_id"], "unit ID")
        release_id = require_safe_id(release["release_id"], "release ID")
        paths.append(
            require_inside(root, f"releases/units/{unit}/{release_id}", "release", must_exist=False)
        )
    for creation_id in plan["creation_ids"]:
        require_safe_id(creation_id, "creation ID")
        paths.append(
            require_inside(
                root, f".pal/transactions/creation/{creation_id}", "创建材料", must_exist=False
            )
        )
    if plan["unit_id"]:
        unit = require_safe_id(plan["unit_id"], "unit ID")
        release_root = require_inside(
            root, f"releases/units/{unit}", "Skill release 目录", must_exist=False
        )
        if {p.name for p in _children(root, f"releases/units/{unit}")} == {
            r["release_id"] for r in plan["releases"] if r["unit_id"] == unit
        }:
            paths.append(release_root)
        paths.append(
            require_inside(root, f"development/units/{unit}", "Skill 开发文件", must_exist=False)
        )
    return paths


def _archive_path(root, version_id):
    require_safe_id(version_id, "production version ID")
    return require_inside(
        root,
        f"records/usage/snapshots/{version_id}",
        "usage 快照证据归档",
        must_exist=False,
    )


def _archive_snapshot(root, version):
    if not version["archive_required"]:
        return
    source = require_inside(
        root,
        f"production/versions/{version['version_id']}",
        "待归档生产快照",
        must_exist=False,
    )
    target = _archive_path(root, version["version_id"])
    if target.exists():
        if not target.is_dir() or tree_digest(target) != version["tree_sha256"]:
            raise IntegrityError("usage 快照证据归档已漂移")
        return
    if not source.exists():
        raise IntegrityError("待归档生产快照已丢失，无法保留 usage 证据")
    validate_regular_tree(source)
    if tree_digest(source) != version["tree_sha256"]:
        raise IntegrityError("待归档生产快照已漂移")
    target.parent.mkdir(parents=True, exist_ok=True)
    os.rename(source, target)
    fsync_directory(target.parent)
    fsync_directory(source.parent)


def _prepare(root, config, library_id, kind, object_id):
    if kind != "skill":
        raise ValueError("历史快照管理已退出；仅支持删除开发内容")
    require_safe_id(object_id, "删除对象 ID")
    doctor_library(root)
    unit_path = require_inside(root, f"development/units/{object_id}/unit.json", "Skill")
    unit = load_json_object(unit_path)
    validate_instance("unit.schema.json", unit)
    if unit["unit_id"] != object_id:
        raise IntegrityError("Skill 目录与标识不一致")
    plan = {
        "kind": "skill",
        "object_id": object_id,
        "unit_id": object_id,
        "versions": [],
        "releases": [],
        "creation_ids": [],
        "revision_count": len(unit["revision_ids"]),
        "claude_cache_root": str(_claude_root()) if _claude_root() else None,
        "usage_references": [],
    }
    retained_releases = set()
    for candidate in _children(root, "production/versions") + _children(
        root, "records/usage/snapshots"
    ):
        validated = validate_production_version(root, candidate.name)
        retained_releases.update((r["unit_id"], r["release_id"]) for r in validated["releases"])
    selected_releases = set()
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


def preview_deletion(library_root, kind, object_id, *, config_root=None):
    with _locked(library_root, config_root) as (root, config, library_id):
        return _prepare(root, config, library_id, kind, object_id)


def _detach_snapshot(library_id, version):
    """Remove only the version-specific official CLI registration."""
    if not version["materialized"]:
        return
    plugin, marketplace = mounts._projection_identity(
        library_id, version["version_id"], stable=version["stable"]
    )
    plan = SimpleNamespace(
        plugin_name=plugin,
        marketplace_name=marketplace,
        version_id=version["version_id"],
        executables=mounts._detect_supported_executables(),
    )
    mounts._remove_claude(plan)
    mounts._remove_codex(plan)
    executable = plan.executables["codex"]
    if any(item.get("name") == marketplace for item in mounts._codex_marketplaces(executable)):
        mounts._run_json(
            [executable, "plugin", "marketplace", "remove", marketplace, "--json"],
            "Codex 历史 marketplace 清理",
        )
    if any(item.get("name") == marketplace for item in mounts._codex_marketplaces(executable)):
        raise IntegrityError("Codex 历史 marketplace 尚未移除")


def _erase(path):
    if path.is_symlink():
        raise IntegrityError(f"不能清理符号链接：{path}")
    if not path.exists():
        return
    if path.is_dir():
        # Every removal is individually resumable. Do not follow links.
        validate_regular_tree(path)
        for child in sorted(path.iterdir()):
            _erase(child)
        path.rmdir()
    elif path.is_file():
        path.unlink()
    else:
        raise IntegrityError(f"不能清理特殊文件：{path}")
    fsync_directory(path.parent)


def _finish(root, config, library_id, journal):
    validate_config_instance(JOURNAL_SCHEMA, journal)
    if (journal["library_root"], journal["config_root"], journal["library_id"]) != (
        str(root),
        str(config),
        library_id,
    ):
        raise IntegrityError("永久删除恢复记录与当前库或配置目录不一致")
    plan = journal["plan"]
    # Older confirmed cleanup journals have no evidence-archive fields.
    for version in plan["versions"]:
        version.setdefault("archive_required", False)
    if plan["unit_id"] != (plan["object_id"] if plan["kind"] == "skill" else None):
        raise IntegrityError("永久删除记录的 Skill 标识不一致")
    if plan["kind"] == "snapshot" and (
        [v["version_id"] for v in plan["versions"]] != [plan["object_id"]]
        or plan["creation_ids"]
        or plan["revision_count"]
    ):
        raise IntegrityError("永久删除记录的历史材料范围不一致")
    _protect_references(root, config, library_id, plan)
    if plan["usage_references"] and any(
        not version["archive_required"] for version in plan["versions"]
    ):
        raise IntegrityError("使用记录引用已变化，不能按原删除范围继续清理")
    paths = _paths(root, config, library_id, plan)
    for path in paths:
        if path.is_dir():
            validate_regular_tree(path)
    for version in plan["versions"]:
        _detach_snapshot(library_id, version)
        _archive_snapshot(root, version)
    for path in _cache_paths(library_id, plan):
        _erase(path)
    for path in paths:
        _erase(path)
    cleanup_path(root).unlink()
    fsync_directory(root / ".pal")
    return {
        "deleted": True,
        "kind": plan["kind"],
        "object_id": plan["object_id"],
        "version_ids": [v["version_id"] for v in plan["versions"]],
        "message": (
            "旧生产材料已清理；使用记录所需的校验证据已保留。"
            if any(v["archive_required"] for v in plan["versions"])
            else "永久删除完成，已清理对应文件，此操作不可撤销。"
        ),
    }


def recover_deletion(library_root, *, config_root=None):
    root = canonical_existing_root(library_root)
    if not cleanup_path(root).exists():
        return {"deleted": False}
    with _locked(root, config_root, recovery=True) as (root, config, library_id):
        if not cleanup_path(root).exists():
            return {"deleted": False}
        return _finish(root, config, library_id, load_json_object(cleanup_path(root)))
