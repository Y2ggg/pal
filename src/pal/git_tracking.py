"""Private Git tracking for a PAL library.

A PAL library is byte-verified, so Git must never rewrite line endings; and the
machine-local runtime state (locks, in-flight transactions, Finder noise) must not
enter history. This module writes the two control files that encode those rules and
optionally initializes the repository and its origin.

Traceability: PRD-P0-002, PRD-P0-005; ACC-001, ACC-012.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .errors import ProductionError
from .library import doctor_library, load_json_object
from .paths import canonical_existing_root
from .platform_support import is_link, run_external

GITIGNORE_CONTENT = """\
.DS_Store
.pal/locks/
.pal/transactions/creation/*
.pal/transactions/usage/*
"""
GITATTRIBUTES_CONTENT = """\
# PAL 以文件字节校验完整性，禁止 Git 做换行转换。
* -text
"""
CONTROL_FILES = {
    ".gitignore": GITIGNORE_CONTENT,
    ".gitattributes": GITATTRIBUTES_CONTENT,
}
NEXT_STEPS_OTHER_MACHINE = (
    "git clone <私有仓 URL> <库路径>",
    "pal adopt --library <库路径>",
    "pal quickstart",
    "pal sync --library <库路径>",
)


def _run_git(
    executable: str,
    arguments: list[str],
    cwd: Path,
    *,
    check: bool = True,
) -> Any:
    result = run_external(
        [executable, *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ProductionError(f"git {' '.join(arguments)} 失败：{detail}")
    return result


def prepare_git_tracking(
    library_root: Path,
    *,
    remote: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Write Git control files, initialize the repository, and optionally commit."""

    root = canonical_existing_root(library_root)
    doctor_library(root)
    written: list[str] = []
    for name, content in CONTROL_FILES.items():
        path = root / name
        if path.exists() or is_link(path):
            if is_link(path) or not path.is_file():
                raise ProductionError(f"{name} 不是普通文件：{path}")
            if path.read_text(encoding="utf-8") != content:
                raise ProductionError(f"{name} 已存在且内容不同；请手工合并后重试")
            continue
        path.write_bytes(content.encode("utf-8"))
        written.append(name)

    executable = shutil.which("git")
    repository = root / ".git"
    initialized = False
    if repository.exists() or is_link(repository):
        if is_link(repository) or not repository.is_dir():
            raise ProductionError(f"库内 .git 不是普通目录：{repository}")
    else:
        if executable is None:
            raise ProductionError("未找到 git 命令；请先安装 Git，再重试 pal git-init")
        _run_git(executable, ["init", "-q"], root)
        initialized = True
    if executable is None:  # pragma: no cover - repository exists without a git binary
        raise ProductionError("未找到 git 命令；请先安装 Git 后再设置远端或提交")

    remote_url: str | None = None
    if remote is not None:
        existing = _run_git(executable, ["remote", "get-url", "origin"], root, check=False)
        if existing.returncode == 0:
            current = (existing.stdout or "").strip()
            if current != remote:
                raise ProductionError(f"origin 已指向 {current}，与传入的远端不同")
            remote_url = current
        else:
            _run_git(executable, ["remote", "add", "origin", remote], root)
            remote_url = remote
    else:
        existing = _run_git(executable, ["remote", "get-url", "origin"], root, check=False)
        if existing.returncode == 0:
            remote_url = (existing.stdout or "").strip()

    committed = False
    if commit:
        _run_git(executable, ["add", "-A"], root)
        status = _run_git(executable, ["status", "--porcelain"], root).stdout or ""
        if status.strip():
            _run_git(executable, ["commit", "-m", "chore: track PAL library"], root)
            committed = True

    manifest = load_json_object(root / "library.json")
    steps: list[str] = []
    if not committed:
        steps.append('git add -A && git commit -m "chore: track PAL library"')
    if remote_url is None:
        steps.append("git remote add origin <私有仓 URL>")
    steps.append("git push -u origin HEAD")
    steps.append("另一台机器：" + " → ".join(NEXT_STEPS_OTHER_MACHINE))
    return {
        "library_id": manifest["library_id"],
        "library_root": str(root),
        "files_written": written,
        "repository_initialized": initialized,
        "remote": remote_url,
        "committed": committed,
        "next_steps": steps,
    }
