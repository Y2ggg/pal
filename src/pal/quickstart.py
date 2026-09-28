"""Guided library setup over the existing fail-closed lifecycle APIs.

Quickstart prepares the library and both creation entries. Skill creation,
publishing, and production consumption stay in their dedicated workflows.

Traceability: PRD-P0-001 through PRD-P0-005, PRD-MOUNT-001,
PRD-MOUNT-002, PRD-CREATE-001; ACC-001, ACC-002, ACC-012; product
decision 051 and requirement 014.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from .adapters import install_creation_adapter
from .compatibility import detect_cli_compatibility
from .config_mount import bind_default_creation_library, default_config_root, mount_config
from .errors import PALError, QuickstartCancelled, QuickstartError, UsageError
from .integrity import check_library_contents
from .library import doctor_library, initialize_library, load_json_object
from .paths import require_safe_id
from .production_mount import validate_production_mount
from .publishing import validate_production_version
from .schema_catalog import TARGET_CLIS

ANSWERS_SCHEMA_VERSION = 2
ANSWER_FIELDS = {
    "schema_version",
    "library_id",
    "library_path",
    "config_root",
}

Input = Callable[[str], str]
Output = Callable[[str], None]


@dataclass(frozen=True)
class QuickstartAnswers:
    """Validated setup inputs; intentionally contains no Skill request or secrets."""

    library_id: str
    library_path: Path
    config_root: Path


def _absolute_path(value: str, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise UsageError(f"{label} must be a non-empty path")
    expanded = Path(value.strip()).expanduser()
    return Path(os.path.abspath(expanded))


def _validate_answers(value: dict[str, Any]) -> QuickstartAnswers:
    schema_version = value.get("schema_version")
    if type(schema_version) is not int:
        raise UsageError("quickstart answers schema_version must be an integer")
    if schema_version == 1:
        raise UsageError(
            "quickstart answers schema_version 1 includes the removed Skill creation workflow; "
            "use schema_version 2 with only library_id, library_path, and config_root"
        )
    if schema_version != ANSWERS_SCHEMA_VERSION:
        raise UsageError("unsupported quickstart answers schema_version")

    unknown = set(value) - ANSWER_FIELDS
    missing = ANSWER_FIELDS - set(value)
    if unknown:
        raise UsageError(f"quickstart answers contain unknown fields: {sorted(unknown)}")
    if missing:
        raise UsageError(f"quickstart answers are missing fields: {sorted(missing)}")

    library_id = require_safe_id(value["library_id"], "library_id")
    library_path = _absolute_path(value["library_path"], "library_path")
    config_root = _absolute_path(value["config_root"], "config_root")
    if library_path.is_relative_to(config_root) or config_root.is_relative_to(library_path):
        raise UsageError("library_path and config_root must not overlap")

    return QuickstartAnswers(
        library_id=library_id,
        library_path=library_path,
        config_root=config_root,
    )


def load_quickstart_answers(path: Path) -> QuickstartAnswers:
    """Load a strict, secret-free setup answer file."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UsageError(f"quickstart answers file does not exist: {path}") from exc
    except UnicodeDecodeError as exc:
        raise UsageError(f"quickstart answers file is not UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise UsageError(f"quickstart answers file is invalid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise UsageError("quickstart answers JSON root must be an object")
    return _validate_answers(value)


def _prompt(input_fn: Input, label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    value = input_fn(f"{label}{suffix}: ").strip()
    if value:
        return value
    if default is not None:
        return default
    raise UsageError(f"{label} is required")


def _prompt_bool(
    input_fn: Input,
    output_fn: Output,
    label: str,
    *,
    default: bool = False,
) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        value = input_fn(f"{label} [{suffix}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes", "是"}:
            return True
        if value in {"n", "no", "否"}:
            return False
        output_fn("输入无效：请输入 y（是）或 n（否）。")


def _automatic_library_id(library_path: Path) -> str:
    """Return an existing identity or derive a stable machine-only ID."""

    manifest_path = library_path / "library.json"
    if manifest_path.is_file() and not manifest_path.is_symlink():
        try:
            return require_safe_id(
                load_json_object(manifest_path).get("library_id"),
                "library_id",
            )
        except PALError:
            # The normal P0 doctor reports the complete existing-library error.
            pass

    name = re.sub(r"(.)([A-Z][a-z]+)", r"\1-\2", library_path.name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
    candidate = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not candidate:
        digest = sha256(str(library_path).encode("utf-8")).hexdigest()[:12]
        candidate = f"library-{digest}"
    return require_safe_id(candidate, "automatically derived library_id")


def collect_quickstart_answers(
    *,
    input_fn: Input = input,
    output_fn: Output = print,
) -> QuickstartAnswers:
    """Collect the one user-supplied setup value and final confirmation."""

    output_fn("PAL · 个人能力库 — 快速开始")
    output_fn("本向导只准备外挂库和 Claude Code/Codex 创建入口。")
    output_fn("不会创建或发布 Skill，不会启动模型，也不会生成使用记录。")
    output_fn("")
    output_fn("[1/1] 外挂库保存位置")
    output_fn("新路径会初始化为空库；已有 PAL 库会校验并继续使用。")
    default_library = Path.home() / "PAL" / "libraries" / "my-agent-plugins"
    library_path = _absolute_path(
        _prompt(input_fn, "保存目录", str(default_library)),
        "library_path",
    )
    answers = _validate_answers(
        {
            "schema_version": ANSWERS_SCHEMA_VERSION,
            "library_id": _automatic_library_id(library_path),
            "library_path": str(library_path),
            "config_root": str(default_config_root()),
        }
    )

    output_fn("")
    output_fn("请确认")
    output_fn(f"- 外挂库：{answers.library_path}")
    output_fn(f"- PAL 配置目录：{answers.config_root}")
    output_fn("- 本次执行：建库/校验、双端配置挂载、双端创建入口准备")
    output_fn("- 不会执行：创建 Skill、调用模型、发布或生成新生产版本")
    if not _prompt_bool(input_fn, output_fn, "以上信息正确，开始设置", default=True):
        raise QuickstartCancelled("已取消，尚未修改任何文件。")
    return answers


def _next_action(stage: str, answers: QuickstartAnswers) -> str:
    base = f"pal quickstart --answers <answers.json>（library: {answers.library_path}）"
    actions = {
        "compatibility": "修复 CLI 兼容性或登录问题后重新运行 " + base,
        "p0": "修复外挂库路径或状态后重新运行 " + base,
        "config-mount": "修复 PAL 配置目录或挂载漂移后重新运行 " + base,
        "creation-adapter": "修复创建入口安装后重新运行 " + base,
        "creation-binding": "修复默认创建库绑定后重新运行 " + base,
        "production": "执行 pal recover 修复生产状态后重新运行 " + base,
        "doctor": "执行 pal doctor 并修复完整性问题后重新运行 " + base,
    }
    return actions[stage]


def _success_next_action(production_ready: bool, library_path: Path) -> str:
    if production_ready:
        return (
            "直接启动 claude 或 codex 使用现有生产 Skill；创建新 Skill 时在会话内调用 "
            "Codex 的 $pal-create-skill 或 Claude Code 的 "
            "pal:pal-create-skill。"
        )
    del library_path
    return (
        "直接启动 claude 或 codex，在会话内调用 Codex 的 $pal-create-skill 或 Claude Code 的 "
        "pal:pal-create-skill，创建后按提示确认发布。"
    )


def run_quickstart(
    answers: QuickstartAnswers,
    *,
    output_fn: Output = print,
) -> dict[str, Any]:
    """Prepare a library and both creation entries without creating a Skill."""

    completed: list[str] = []
    stage = "compatibility"

    def progress(message: str) -> None:
        output_fn(f"[PAL] {message}")

    try:
        compatibility: dict[str, dict[str, Any]] = {}
        progress("检查 Claude Code 与 Codex 兼容性")
        for cli_id in TARGET_CLIS:
            compatibility[cli_id] = detect_cli_compatibility(cli_id).diagnostic()
        completed.append(stage)

        stage = "p0"
        library_exists = answers.library_path.exists() or answers.library_path.is_symlink()
        empty_directory = (
            library_exists
            and answers.library_path.is_dir()
            and not any(answers.library_path.iterdir())
        )
        if library_exists and not empty_directory:
            progress("验证已有外挂库")
            doctor = doctor_library(answers.library_path)
            if doctor["library_id"] != answers.library_id:
                raise UsageError("existing library_id differs from the quickstart answer")
            library_reused = True
        else:
            progress("初始化外挂库（P0）")
            answers.library_path.parent.mkdir(parents=True, exist_ok=True)
            initialize_library(answers.library_path, answers.library_id)
            doctor = doctor_library(answers.library_path)
            library_reused = False
        completed.append(stage)

        stage = "config-mount"
        progress("建立 Claude Code 与 Codex 的独立创建配置挂载")
        config_mounts = {
            cli_id: mount_config(
                answers.library_path,
                cli_id,
                config_root=answers.config_root,
            )
            for cli_id in TARGET_CLIS
        }
        completed.append(stage)

        stage = "creation-adapter"
        progress("安装 Claude Code 与 Codex 创建入口")
        creation_adapters = {
            cli_id: install_creation_adapter(
                answers.library_path,
                cli_id,
                config_root=answers.config_root,
            )
            for cli_id in TARGET_CLIS
        }
        completed.append(stage)

        stage = "creation-binding"
        progress("保存默认创建库")
        default_creation_library = bind_default_creation_library(
            answers.library_path,
            config_root=answers.config_root,
        )
        completed.append(stage)

        active_version_id = doctor["active_production_version_id"]
        activation: dict[str, Any] | None = None
        if active_version_id is not None:
            stage = "production"
            progress("检查已有 CLI 挂载（不发布或同步业务 Skill）")
            activation = validate_production_mount(
                answers.library_path,
                active_version_id,
                config_root=answers.config_root,
            )
            completed.append(stage)
        else:
            progress("当前没有活动生产版本；已准备好创建第一个真实业务 Skill")

        stage = "doctor"
        progress("执行最终完整性检查")
        final_doctor = check_library_contents(answers.library_path)
        completed.append(stage)
        # An active v2 production may intentionally be empty after the user
        # removes every Skill from the production set.  It is a valid mounted
        # state, but it is not "production ready" for consumption because no
        # Skill can be used from it.
        production_ready = False
        if active_version_id is not None:
            active_production = validate_production_version(answers.library_path, active_version_id)
            production_ready = bool(active_production["releases"])
        return {
            "completed": True,
            "setup_status": "production-ready" if production_ready else "creation-ready",
            "library_id": answers.library_id,
            "library_root": str(answers.library_path),
            "config_root": str(answers.config_root),
            "library_reused": library_reused,
            "creation_ready": True,
            "production_ready": production_ready,
            "active_production_version_id": active_version_id,
            "plugin_name": activation["plugin_name"] if activation is not None else None,
            "compatibility": compatibility,
            "config_mounts": config_mounts,
            "creation_adapters": creation_adapters,
            "default_creation_library": default_creation_library,
            "doctor": final_doctor,
            "next_action": _success_next_action(production_ready, answers.library_path),
        }
    except QuickstartError:
        raise
    except (PALError, OSError) as exc:
        raise QuickstartError(
            str(exc),
            stage=stage,
            completed_stages=tuple(completed),
            next_action=_next_action(stage, answers),
        ) from exc


__all__ = [
    "ANSWER_FIELDS",
    "ANSWERS_SCHEMA_VERSION",
    "QuickstartAnswers",
    "collect_quickstart_answers",
    "load_quickstart_answers",
    "run_quickstart",
]
