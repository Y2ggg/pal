"""Portable library archives: verified export and same-machine adoption on import.

A PAL library holds only relative references, but its active pointer records an
absolute machine-local bundle path. Moving the library to another machine therefore
needs two explicit steps: carry the bytes, then re-bind the pointer to the local
config root. Both steps stay fail closed: an archive is fully verified before it is
unpacked, a diverged library is never overwritten, and a present-but-drifting mount
bundle is never silently replaced.

Traceability: PRD-P0-002, PRD-P0-003, PRD-P0-005; ACC-001, ACC-012.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import secrets
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from . import __version__
from .errors import IntegrityError, PathSafetyError, ProductionError
from .io import (
    formatted_json_bytes,
    fsync_directory,
    sha256_bytes,
    sha256_file,
)
from .library import doctor_library, load_json_object, utc_now
from .paths import canonical_existing_root, normalize_relative_path, require_safe_id
from .platform_support import is_link, move_path
from .production_mount import adopt_active_production

ARCHIVE_FORMAT = "pal-library-archive/1"
ARCHIVE_MANIFEST = "manifest.json"
ARCHIVE_LIBRARY_PREFIX = "library/"
LOCK_DIRECTORY = ".pal/locks"
TRANSACTION_DIRECTORY = ".pal/transactions"
TRANSACTION_DIRECTORIES = (
    f"{TRANSACTION_DIRECTORY}/creation",
    f"{TRANSACTION_DIRECTORY}/usage",
)
TERMINAL_TRANSACTION_STATES = frozenset({"COMMITTED", "ABORTED", "COMPLETED"})
EXCLUDED_FILE_NAMES = frozenset({".DS_Store"})
GIT_METADATA_DIRECTORY = ".git"
IMMUTABLE_PREFIXES = (
    "releases/",
    "production/versions/",
    "schemas/",
    "development/specifications/",
    "development/design-rules/",
    "records/",
)
IMMUTABLE_UNIT_MARKER = "/revisions/"


def _archive_tree_digest(entries: list[dict[str, Any]]) -> str:
    material = "".join(
        f"{entry['path']}\0{entry['sha256']}\0{entry['size']}\n" for entry in entries
    )
    return sha256_bytes(material.encode("utf-8"))


def _join(relative_directory: str, name: str) -> str:
    if relative_directory in {"", "."}:
        return name
    return f"{relative_directory}/{name}"


def _is_excluded_directory(relative_directory: str, name: str) -> bool:
    relative = _join(relative_directory, name)
    return relative in {LOCK_DIRECTORY, TRANSACTION_DIRECTORY, GIT_METADATA_DIRECTORY}


def _is_excluded_file(relative: str) -> bool:
    if relative == GIT_METADATA_DIRECTORY:
        return True
    return relative.startswith(f"{LOCK_DIRECTORY}/") or relative.startswith(
        f"{TRANSACTION_DIRECTORY}/"
    )


def _relative_paths(root: Path) -> list[tuple[str, Path]]:
    """List regular files in deterministic order, excluding machine-local state."""

    found: list[tuple[str, Path]] = []
    for current, directory_names, file_names in os.walk(root):
        current_path = Path(current)
        relative_directory = current_path.relative_to(root).as_posix()
        directory_names[:] = sorted(
            name for name in directory_names if not _is_excluded_directory(relative_directory, name)
        )
        for name in sorted(file_names):
            if name in EXCLUDED_FILE_NAMES:
                continue
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if _is_excluded_file(relative):
                continue
            found.append((relative, path))
    found.sort(key=lambda item: item[0])
    return found


def _relative_directories(root: Path) -> list[str]:
    """List directories in deterministic order, excluding machine-local state."""

    found: list[str] = []
    for current, directory_names, _file_names in os.walk(root):
        current_path = Path(current)
        relative_directory = current_path.relative_to(root).as_posix()
        directory_names[:] = sorted(
            name for name in directory_names if not _is_excluded_directory(relative_directory, name)
        )
        for name in directory_names:
            found.append(_join(relative_directory, name))
    found.sort()
    return found


def _require_no_open_transactions(root: Path) -> None:
    directory = root / TRANSACTION_DIRECTORY
    if not directory.is_dir():
        return
    for path in sorted(directory.rglob("transaction.json")):
        transaction = load_json_object(path)
        state = transaction.get("state")
        if state not in TERMINAL_TRANSACTION_STATES:
            raise ProductionError(f"存在未完成的事务 {state}：{path}；请先完成或恢复后再导出")


def _tar_info(name: str, *, directory: bool, size: int = 0) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name + "/" if directory else name)
    info.mtime = 0
    info.mode = 0o755 if directory else 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.size = 0 if directory else size
    return info


def _write_archive(
    output: Path,
    manifest_bytes: bytes,
    root: Path,
    entries: list[dict[str, Any]],
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with (
            temporary.open("wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
            tarfile.open(fileobj=compressed, mode="w") as archive,
        ):
            archive.addfile(
                _tar_info(ARCHIVE_MANIFEST, directory=False, size=len(manifest_bytes)),
                io.BytesIO(manifest_bytes),
            )
            for relative in _relative_directories(root):
                archive.addfile(_tar_info(f"{ARCHIVE_LIBRARY_PREFIX}{relative}", directory=True))
            for relative in TRANSACTION_DIRECTORIES:
                archive.addfile(_tar_info(f"{ARCHIVE_LIBRARY_PREFIX}{relative}", directory=True))
            for entry in entries:
                with (root / entry["path"]).open("rb") as handle:
                    archive.addfile(
                        _tar_info(
                            f"{ARCHIVE_LIBRARY_PREFIX}{entry['path']}",
                            directory=False,
                            size=entry["size"],
                        ),
                        handle,
                    )
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, output)
        fsync_directory(output.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def export_library(library_root: Path, output: Path) -> dict[str, Any]:
    """Write one fully verified, self-describing archive of the library."""

    root = canonical_existing_root(library_root)
    doctor_library(root)
    _require_no_open_transactions(root)
    destination = Path(os.path.abspath(output))
    if destination == root or root in destination.parents:
        raise ProductionError("导出归档不能写进库目录；请选择库外路径")
    manifest = load_json_object(root / "library.json")
    library_id = require_safe_id(manifest["library_id"], "library_id")
    entries: list[dict[str, Any]] = []
    for relative, path in _relative_paths(root):
        entries.append({"path": relative, "sha256": sha256_file(path), "size": path.stat().st_size})
    active = load_json_object(root / "production" / "active.json")
    archive_manifest = {
        "archive_format": ARCHIVE_FORMAT,
        "library_id": library_id,
        "pal_format_version": manifest["pal_format_version"],
        "pal_version": __version__,
        "exported_at": utc_now(),
        "active_production_version_id": active["active_production_version_id"],
        "file_count": len(entries),
        "tree_sha256": _archive_tree_digest(entries),
        "files": entries,
    }
    _write_archive(
        destination,
        formatted_json_bytes(archive_manifest),
        root,
        entries,
    )
    return {
        "library_id": library_id,
        "library_root": str(root),
        "archive": str(destination),
        "archive_sha256": sha256_file(destination),
        "archive_bytes": destination.stat().st_size,
        "file_count": len(entries),
        "tree_sha256": archive_manifest["tree_sha256"],
        "active_production_version_id": active["active_production_version_id"],
    }


def _verify_archive_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if manifest.get("archive_format") != ARCHIVE_FORMAT:
        raise IntegrityError("归档格式不受支持")
    require_safe_id(manifest.get("library_id"), "library_id")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise IntegrityError("归档清单缺少文件列表")
    entries: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            raise IntegrityError("归档清单文件项必须是对象")
        relative = normalize_relative_path(item.get("path"), "归档文件路径")
        digest = item.get("sha256")
        size = item.get("size")
        if not isinstance(digest, str) or len(digest) != 64:
            raise IntegrityError(f"归档文件摘要无效：{relative}")
        if not isinstance(size, int) or size < 0:
            raise IntegrityError(f"归档文件大小无效：{relative}")
        entries.append({"path": relative, "sha256": digest, "size": size})
    if len({entry["path"] for entry in entries}) != len(entries):
        raise IntegrityError("归档清单存在重复路径")
    if manifest.get("tree_sha256") != _archive_tree_digest(entries):
        raise IntegrityError("归档清单树摘要与文件列表不一致")
    if manifest.get("file_count") != len(entries):
        raise IntegrityError("归档清单文件数量与文件列表不一致")
    return entries


def _is_immutable(relative: str) -> bool:
    if relative.startswith("development/units/") and IMMUTABLE_UNIT_MARKER in relative:
        return True
    return any(relative.startswith(prefix) for prefix in IMMUTABLE_PREFIXES)


def _require_no_divergence(existing: Path, entries: list[dict[str, Any]]) -> None:
    archived = {entry["path"]: entry["sha256"] for entry in entries}
    diverged: list[str] = []
    for relative, path in _relative_paths(existing):
        if not _is_immutable(relative):
            continue
        expected = archived.get(relative)
        if expected is None:
            diverged.append(f"{relative}（本机新增，归档中没有）")
        elif expected != sha256_file(path):
            diverged.append(f"{relative}（内容与归档不同）")
        if len(diverged) >= 5:
            break
    if diverged:
        raise ProductionError(
            "目标库与归档在不可变内容上不一致，疑似两台机器各有新发布；"
            "请先在源机器导出最新库，或手工比对后再替换。差异：" + "；".join(diverged)
        )


def _unpack(archive_path: Path, staging: Path) -> tuple[dict[str, Any], int]:
    if not archive_path.is_file():
        raise ProductionError(f"找不到归档文件：{archive_path}")
    staging.mkdir(parents=True)
    manifest: dict[str, Any] | None = None
    seen: set[str] = set()
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive:
                if member.issym() or member.islnk() or member.isdev():
                    raise PathSafetyError(f"归档包含不允许的成员类型：{member.name}")
                if member.isdir():
                    if member.name.rstrip("/") == ARCHIVE_LIBRARY_PREFIX.rstrip("/"):
                        continue
                    if not member.name.startswith(ARCHIVE_LIBRARY_PREFIX):
                        raise PathSafetyError(f"归档目录成员不在 library/ 下：{member.name}")
                    relative = normalize_relative_path(
                        member.name[len(ARCHIVE_LIBRARY_PREFIX) :], "归档目录路径"
                    )
                    (staging / relative).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise PathSafetyError(f"归档包含不支持的成员：{member.name}")
                if member.name == ARCHIVE_MANIFEST:
                    handle = archive.extractfile(member)
                    if handle is None:  # pragma: no cover - regular file
                        raise IntegrityError("无法读取归档清单")
                    try:
                        manifest = json.loads(handle.read().decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise IntegrityError(f"归档清单不是合法 JSON：{exc}") from exc
                    continue
                if not member.name.startswith(ARCHIVE_LIBRARY_PREFIX):
                    raise PathSafetyError(f"归档包含未知成员：{member.name}")
                relative = normalize_relative_path(
                    member.name[len(ARCHIVE_LIBRARY_PREFIX) :], "归档文件路径"
                )
                if relative in seen:
                    raise IntegrityError(f"归档包含重复文件：{relative}")
                seen.add(relative)
                handle = archive.extractfile(member)
                if handle is None:  # pragma: no cover - regular file
                    raise IntegrityError(f"无法读取归档文件：{relative}")
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(handle.read())
        if manifest is None:
            raise IntegrityError("归档缺少 manifest.json")
        entries = _verify_archive_manifest(manifest)
        expected = {entry["path"] for entry in entries}
        if seen != expected:
            missing = sorted(expected - seen)
            extra = sorted(seen - expected)
            detail = []
            if missing:
                detail.append("缺少 " + "、".join(missing[:5]))
            if extra:
                detail.append("多出 " + "、".join(extra[:5]))
            raise IntegrityError("归档内容与清单不一致：" + "；".join(detail))
        for entry in entries:
            path = staging / entry["path"]
            if path.stat().st_size != entry["size"]:
                raise IntegrityError(f"归档文件大小不符：{entry['path']}")
            if sha256_file(path) != entry["sha256"]:
                raise IntegrityError(f"归档文件摘要不符：{entry['path']}")
        return manifest, len(entries)
    except (tarfile.TarError, OSError) as exc:
        raise ProductionError(f"归档无法读取：{exc}") from exc


def _backup_existing(target: Path) -> Path:
    stamp = "".join(character for character in utc_now() if character.isalnum())
    candidate = target.parent / f"{target.name}.replaced-{stamp}"
    suffix = 1
    while candidate.exists() or is_link(candidate):
        candidate = target.parent / f"{target.name}.replaced-{stamp}-{suffix}"
        suffix += 1
    move_path(target, candidate)
    return candidate


def import_library(
    archive_path: Path,
    library_root: Path,
    *,
    config_root: Path | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Verify one archive into a new library path and adopt it on this machine."""

    archive = Path(os.path.abspath(archive_path))
    target = Path(os.path.abspath(library_root))
    staging = target.parent / f".{target.name}.importing-{secrets.token_hex(4)}"
    backup: Path | None = None
    if target.exists() or is_link(target):
        if not replace:
            raise ProductionError(f"目标库路径已存在：{target}；如需替换请显式使用 --replace")
        if is_link(target) or not target.is_dir():
            raise PathSafetyError(f"目标库路径不是普通目录：{target}")
    manifest, file_count = _unpack(archive, staging)
    moved = False
    try:
        if target.exists() or is_link(target):
            _require_no_divergence(target, manifest["files"])
            backup = _backup_existing(target)
        move_path(staging, target)
        moved = True
        fsync_directory(target.parent)
        (target / LOCK_DIRECTORY).mkdir(parents=True, exist_ok=True)
        for relative in TRANSACTION_DIRECTORIES:
            (target / relative).mkdir(parents=True, exist_ok=True)
        active = load_json_object(target / "production" / "active.json")
        adopted: dict[str, Any] | None = None
        if active["active_production_version_id"] is not None:
            adopted = adopt_active_production(target, config_root=config_root)
        report = doctor_library(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if moved:
            shutil.rmtree(target, ignore_errors=True)
        if backup is not None and not target.exists():
            move_path(backup, target)
        raise
    return {
        "library_id": report["library_id"],
        "library_root": str(target),
        "archive": str(archive),
        "file_count": file_count,
        "tree_sha256": manifest["tree_sha256"],
        "active_production_version_id": report.get("active_production_version_id"),
        "adopted": adopted is not None,
        "active_pointer_rewritten": (
            adopted["active_pointer_rewritten"] if adopted is not None else None
        ),
        "previous_backup": str(backup) if backup is not None else None,
        "next_action": "pal sync --library <库路径>" if adopted is not None else None,
    }
