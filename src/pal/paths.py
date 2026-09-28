"""PAL identifier and path-boundary validation.

Traceability: PRD-P0-001 through PRD-P0-005; ACC-001, ACC-012.
"""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from pathlib import Path, PurePosixPath

from .errors import InitializationError, PathSafetyError

SAFE_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"
SAFE_ID = re.compile(SAFE_ID_PATTERN)


def require_safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or SAFE_ID.fullmatch(value) is None:
        raise PathSafetyError(f"{label} must match {SAFE_ID_PATTERN}")
    return value


def normalize_relative_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PathSafetyError(f"{label} must be a non-empty relative path")
    if unicodedata.normalize("NFC", value) != value:
        raise PathSafetyError(f"{label} must use UTF-8 NFC path spelling")
    if "\\" in value or any(ord(character) < 32 for character in value):
        raise PathSafetyError(f"{label} contains a forbidden character")
    if value.startswith("/") or "//" in value:
        raise PathSafetyError(f"{label} must be a normalized relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PathSafetyError(f"{label} contains an unsafe path segment")
    if PurePosixPath(value).as_posix() != value:
        raise PathSafetyError(f"{label} is not a normalized POSIX path")
    return value


def canonical_init_target(path: Path) -> Path:
    """Resolve the target parent while preserving and checking the target leaf."""

    absolute = Path(os.path.abspath(path))
    if absolute == Path(absolute.anchor) or not absolute.name:
        raise InitializationError("library root cannot be a filesystem root")
    if absolute.is_symlink():
        raise InitializationError(f"library root cannot be a symbolic link: {absolute}")
    try:
        parent = absolute.parent.resolve(strict=True)
    except FileNotFoundError as exc:
        raise InitializationError(f"library parent does not exist: {absolute.parent}") from exc
    if not parent.is_dir():
        raise InitializationError(f"library parent is not a directory: {parent}")
    return parent / absolute.name


def canonical_existing_root(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    if absolute.is_symlink():
        raise PathSafetyError(f"library root cannot be a symbolic link: {absolute}")
    try:
        resolved = absolute.resolve(strict=True)
    except FileNotFoundError as exc:
        raise PathSafetyError(f"library root does not exist: {absolute}") from exc
    if not resolved.is_dir():
        raise PathSafetyError(f"library root is not a directory: {resolved}")
    return resolved


def require_inside(
    root: Path,
    relative_path: object,
    label: str,
    *,
    must_exist: bool = True,
) -> Path:
    relative = normalize_relative_path(relative_path, label)
    root = root.resolve(strict=True)
    cursor = root
    for part in relative.split("/"):
        cursor = cursor / part
        if cursor.is_symlink():
            raise PathSafetyError(f"{label} contains a symbolic link: {cursor}")
    try:
        resolved = cursor.resolve(strict=must_exist)
    except FileNotFoundError as exc:
        raise PathSafetyError(f"{label} does not exist: {cursor}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PathSafetyError(f"{label} escapes library root: {resolved}") from exc
    return resolved


def validate_regular_tree(root: Path) -> None:
    """Reject links, special files, unsafe spellings, and normalized-name collisions."""

    for directory, names, files in os.walk(root, followlinks=False):
        current = Path(directory)
        normalized: set[str] = set()
        for name in [*names, *files]:
            if unicodedata.normalize("NFC", name) != name:
                raise PathSafetyError(f"path name is not NFC-normalized: {current / name}")
            if "\\" in name or any(ord(character) < 32 for character in name):
                raise PathSafetyError(f"path name contains a forbidden character: {current / name}")
            normalized_name = unicodedata.normalize("NFC", name)
            if normalized_name in normalized:
                raise PathSafetyError(f"normalized path collision below: {current}")
            normalized.add(normalized_name)
            path = current / name
            if path.is_symlink():
                raise PathSafetyError(f"library contains a symbolic link: {path}")
            mode = path.stat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise PathSafetyError(f"library contains a special file: {path}")
