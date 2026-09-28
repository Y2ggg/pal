"""Coordinate irreversible cleanup with ordinary lifecycle writers.

Traceability: DEL-004/005; PRD-RELEASE-002; ACC-012.
"""

import os
import threading
from contextlib import contextmanager
from functools import wraps
from inspect import signature
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - unsupported platform
    fcntl = None

from .errors import IntegrityError
from .paths import canonical_existing_root, require_inside

_held = threading.local()


def cleanup_path(root: Path) -> Path:
    return require_inside(root, ".pal/cleanup.json", "永久删除恢复记录", must_exist=False)


def skill_action_path(root: Path) -> Path:
    return require_inside(root, ".pal/skill-action.json", "Skill 操作恢复记录", must_exist=False)


def require_no_cleanup(root: Path) -> None:
    if skill_action_path(root).exists():
        raise IntegrityError("Skill 删除尚未完成，请到「库与维护」执行异常恢复")
    if cleanup_path(root).exists():
        raise IntegrityError("永久删除尚未完成，请到「库与维护」执行异常恢复，继续清理")


@contextmanager
def maintenance_lock(root: Path, *, exclusive: bool = False, recovery: bool = False):
    if fcntl is None:
        raise IntegrityError("维护操作需要 POSIX 文件锁支持")
    root = canonical_existing_root(root)
    held = getattr(_held, "roots", {})
    if root in held:
        if exclusive and not held[root]:
            raise IntegrityError("不能在普通操作中升级为永久删除")
        yield
        return
    path = require_inside(root, ".pal/locks/maintenance.lock", "维护锁", must_exist=False)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise IntegrityError("外挂库正在执行其他操作，请稍后重试") from exc
        if not recovery:
            require_no_cleanup(root)
        _held.roots = {**held, root: exclusive}
        try:
            yield
        finally:
            _held.roots = held
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def lifecycle_write(function):
    """Share the maintenance gate across nested existing lifecycle functions."""

    root_parameter = next(iter(signature(function).parameters))

    @wraps(function)
    def guarded(*args, **kwargs):
        library_root = args[0] if args else kwargs[root_parameter]
        with maintenance_lock(library_root):
            return function(*args, **kwargs)

    return guarded
