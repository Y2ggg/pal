"""Deterministic JSON, digest, and durable filesystem primitives.

Traceability: PRD-P0-002, PRD-P0-003, PRD-TECH-001; ACC-001, ACC-012.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from .errors import IntegrityError, PathSafetyError
from .platform_support import is_link, move_path


def canonical_json_bytes(value: Any) -> bytes:
    """Return the canonical JSON bytes used as digest material."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def formatted_json_bytes(value: Any) -> bytes:
    """Return PAL's deterministic persisted JSON representation."""

    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        # Windows has no directory fsync; move_path uses same-volume WRITE_THROUGH.
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_new_bytes(path: Path, value: bytes) -> None:
    """Create and durably write a file; never replace an existing file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def write_new_json(path: Path, value: Any) -> None:
    write_new_bytes(path, formatted_json_bytes(value))


def atomic_replace_json(path: Path, value: Any) -> None:
    """Durably replace one JSON file through a same-directory temporary file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(formatted_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        move_path(temporary, path, replace=True)
        fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _tree_entries(root: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    normalized_names: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        normalized = unicodedata.normalize("NFC", relative)
        if normalized != relative:
            raise PathSafetyError(f"path is not NFC-normalized: {relative}")
        if normalized in normalized_names:
            raise PathSafetyError(f"normalized path collision: {relative}")
        normalized_names.add(normalized)
        if is_link(path):
            raise PathSafetyError(f"tree contains a symbolic link: {path}")
        mode = path.stat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise PathSafetyError(f"tree contains a non-regular file: {path}")
        entries.append({"path": relative, "sha256": sha256_file(path)})
    return sorted(entries, key=lambda item: item["path"].encode("utf-8"))


def tree_digest(root: Path) -> str:
    """Hash the canonical sorted file manifest for a tree."""

    if is_link(root) or not root.is_dir():
        raise PathSafetyError(f"tree root must be a real directory: {root}")
    return sha256_bytes(canonical_json_bytes(_tree_entries(root)))


def fsync_tree(root: Path) -> None:
    """Synchronize every regular file and directory below ``root``."""

    directories = [root]
    for path in root.rglob("*"):
        if is_link(path):
            raise PathSafetyError(f"tree contains a symbolic link: {path}")
        mode = path.stat().st_mode
        if stat.S_ISDIR(mode):
            directories.append(path)
        elif stat.S_ISREG(mode):
            descriptor = os.open(path, os.O_RDWR if os.name == "nt" else os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        else:
            raise PathSafetyError(f"tree contains a non-regular file: {path}")
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        fsync_directory(directory)


def require_digest(path: Path, expected_sha256: str, label: str) -> None:
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise IntegrityError(f"{label} SHA-256 mismatch: expected {expected_sha256}, got {actual}")
