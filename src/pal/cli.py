"""The initial PAL command surface.

Traceability: PRD-P0-001 through PRD-P0-005, PRD-TECH-001; ACC-001, ACC-012;
WEB-001 through WEB-006.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from . import __version__
from .adapters import launch_creation_entry, prepare_creation_adapter
from .build_identity import BUILD_ID
from .compatibility import detect_cli_compatibility
from .config_mount import mount_config, resolve_config_root
from .creation import (
    abort_creation,
    begin_creation,
    commit_creation,
    inspect_creation,
    list_creation_units,
    resolve_library_root,
)
from .deletion import recover_deletion
from .errors import PALError, QuickstartCancelled, QuickstartError, UsageError
from .integrity import check_library_contents
from .library import initialize_library
from .production_mount import (
    launch_production_entry,
    recover_production,
)
from .publication import (
    migrate_production_namespace,
    publish_unit,
    sync_production,
)
from .publishing import compose_production, create_release
from .quickstart import (
    collect_quickstart_answers,
    load_quickstart_answers,
    run_quickstart,
)
from .skill_actions import ACTIONS, execute_skill_action, preview_skill_action, recover_skill_action
from .status import library_status
from .usage import (
    recover_usage_transactions,
    run_production_task,
    validate_usage_record,
)
from .web import DEFAULT_HOST, DEFAULT_PORT, run_web, stop_web

REJECTION_EXIT_CODE = 20


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="PAL · 个人能力库：跨 Claude Code / Codex 复用 Skill（命令 pal）",
        epilog="日常：quickstart 配置一次 → 直接打开 claude / codex 创建、更新和使用 Skill。\n"
        "管理：web 打开控制台；publish 手动发布到生产；sync 手动同步到 CLI；doctor 检查完整性。\n"
        "底层/自动化命令（非日常必经）：init、mount、create、release、skill、production、recover。\n"
        "兼容/诊断：compatibility、launch、run、usage。各命令后加 --help 查看参数。\n"
        "日常记录与评分暂缓；完整性检查不代表 Skill 效果测试。",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__} (build {BUILD_ID})"
    )
    commands = parser.add_subparsers(
        dest="command", required=True, title="常用命令", metavar="命令"
    )

    quickstart = commands.add_parser(
        "quickstart",
        help="初始化或复用外挂库，并准备 Claude Code/Codex 创建入口",
    )
    quickstart.add_argument(
        "--answers",
        type=Path,
        help="从 JSON 文件读取严格的非交互设置参数",
    )

    init = commands.add_parser(
        "init",
        help="初始化外挂库",
    )
    init.add_argument("--library", required=True, type=Path)
    init.add_argument("--library-id", required=True)

    doctor = commands.add_parser(
        "doctor",
        help="检查外挂库完整性（不执行 Skill）",
    )
    doctor.add_argument("--library", required=True, type=Path)

    compatibility = commands.add_parser(
        "compatibility",
        help="只读检查本机目标 CLI 的兼容能力",
    )
    compatibility_commands = compatibility.add_subparsers(
        dest="compatibility_command",
        required=True,
    )
    compatibility_check = compatibility_commands.add_parser(
        "check",
        help="检查版本、命令协议与隔离行为",
    )
    compatibility_check.add_argument("--cli", required=True)

    mount = commands.add_parser(
        "mount",
        help="管理 PAL 配置挂载或同步当前生产",
    )
    mount_commands = mount.add_subparsers(dest="mount_command", required=True)
    config = mount_commands.add_parser(
        "config",
        help="向目标 CLI 提供外挂库创建规范与位置",
    )
    config.add_argument("--library", required=True, type=Path)
    # Choices are checked in the domain layer so an invalid CLI is a normal
    # PAL rejection (exit 20), rather than argparse's usage exit (2).
    config.add_argument("--cli", required=True)
    config.add_argument("--config-root", type=Path)
    production_mount = mount_commands.add_parser(
        "production",
        help="同步当前生产到 CLI（兼容入口）",
    )
    production_mount.add_argument("--library", type=Path)
    production_mount.add_argument("--version", required=True)
    production_mount.add_argument("--config-root", type=Path)

    create = commands.add_parser(
        "create",
        help="管理 Skill 创建与更新事务",
    )
    create_commands = create.add_subparsers(dest="create_command", required=True)
    inspect = create_commands.add_parser(
        "inspect", help="读取已有 Skill 的当前开发版本，供 CLI 更新"
    )
    inspect.add_argument("--library", type=Path)
    inspect.add_argument("--config-root", type=Path)
    inspect.add_argument("--unit", required=True)
    list_units = create_commands.add_parser(
        "list", help="列出可选择更新的 Skill 及其生产状态（只读）"
    )
    list_units.add_argument("--library", type=Path)
    list_units.add_argument("--config-root", type=Path)
    begin = create_commands.add_parser(
        "begin",
        help="准备跨 CLI 创建或更新事务",
    )
    begin.add_argument("--library", type=Path)
    begin.add_argument("--cli", required=True)
    begin.add_argument("--request-file", required=True, type=Path)
    begin.add_argument("--config-root", type=Path)

    commit = create_commands.add_parser(
        "commit",
        help="校验并保存开发内容",
    )
    commit.add_argument("--library", type=Path)
    commit.add_argument("--creation", required=True)
    commit.add_argument("--config-root", type=Path)

    abort = create_commands.add_parser(
        "abort",
        help="中止尚未提交的创建事务",
    )
    abort.add_argument("--library", type=Path)
    abort.add_argument("--creation", required=True)
    abort.add_argument("--config-root", type=Path)

    release = commands.add_parser(
        "release",
        help="生成内部不可变发布材料（底层自动化接口）",
    )
    release_commands = release.add_subparsers(dest="release_command", required=True)
    release_create = release_commands.add_parser(
        "create",
        help="从已提交开发修订生成发布材料",
    )
    release_create.add_argument("--library", type=Path)
    release_create.add_argument("--unit", required=True)
    release_create.add_argument("--revision", required=True)

    publish = commands.add_parser(
        "publish",
        help="发布选定 Skill 到生产，默认不改变 CLI 挂载",
    )
    publish.add_argument("--library", type=Path)
    publish.add_argument("--unit", required=True)
    publish.add_argument("--config-root", type=Path)
    publish.add_argument("--sync", action="store_true", help="明确同时执行发布和 CLI 同步")
    sync = commands.add_parser("sync", help="将当前已发布内容手动同步到 Claude Code/Codex")
    sync.add_argument("--library", type=Path)
    sync.add_argument("--config-root", type=Path)

    production = commands.add_parser(
        "production",
        help="查询三层状态或维护生产调用名",
    )
    production_commands = production.add_subparsers(
        dest="production_command",
        required=True,
    )
    migrate = production_commands.add_parser(
        "migrate-names", help="仅迁移当前生产的调用名，不发布开发更新"
    )
    migrate.add_argument("--library", type=Path)
    migrate.add_argument("--config-root", type=Path)
    migrate.add_argument("--sync", action="store_true", help="明确同时同步到 CLI")
    status = production_commands.add_parser("status", help="查看已发布与已挂载状态")
    status.add_argument("--library", type=Path)
    status.add_argument("--config-root", type=Path)
    compose = production_commands.add_parser(
        "compose",
        help="组成明确指定的发布材料，不同步到 CLI（底层自动化接口）",
    )
    compose.add_argument("--library", type=Path)
    compose.add_argument("--release", required=True, action="append", dest="release_ids")
    activate = production_commands.add_parser(
        "activate",
        help="同步当前生产到 CLI，新会话生效（兼容入口）",
    )
    activate.add_argument("--library", type=Path)
    activate.add_argument("--version", required=True)
    activate.add_argument("--config-root", type=Path)
    production_commands._choices_actions[:] = [
        action
        for action in production_commands._choices_actions
        if action.dest in {"status", "migrate-names"}
    ]
    production_commands.metavar = "操作"

    recover = commands.add_parser(
        "recover",
        help="恢复中断的 Skill 删除、清理、挂载或受控使用事务",
    )
    recover.add_argument("--library", type=Path)
    recover.add_argument("--config-root", type=Path)

    run = commands.add_parser(
        "run",
        help="执行一次受控任务并记录关联（可选诊断）",
    )
    run.add_argument("--library", type=Path)
    run.add_argument("--cli", required=True)
    run.add_argument("--task-file", required=True)
    run.add_argument("--config-root", type=Path)

    usage = commands.add_parser(
        "usage",
        help="校验已有受控使用记录（兼容维护）",
    )
    usage_commands = usage.add_subparsers(dest="usage_command", required=True)
    usage_validate = usage_commands.add_parser(
        "validate",
        help="校验已有使用记录及其不可变内容",
    )
    usage_validate.add_argument("--library", type=Path)
    usage_validate.add_argument("--record", required=True, type=Path)
    usage_validate.add_argument("--config-root", type=Path)

    launch = commands.add_parser(
        "launch",
        help="通过兼容入口启动 CLI，日常可直接使用 CLI",
    )
    launch_commands = launch.add_subparsers(dest="launch_command", required=True)
    launch_create = launch_commands.add_parser(
        "create",
        help="准备系统创建入口并启动 CLI（兼容入口）",
    )
    launch_create.add_argument("--library", type=Path)
    launch_create.add_argument("--cli", required=True)
    launch_create.add_argument("--config-root", type=Path)
    launch_create.add_argument("--prepare-only", action="store_true")
    launch_create.add_argument("cli_arguments", nargs=argparse.REMAINDER)
    launch_use = launch_commands.add_parser(
        "use",
        help="启动使用已挂载内容的新 CLI 会话（兼容入口）",
    )
    launch_use.add_argument("--library", type=Path)
    launch_use.add_argument("--cli", required=True)
    launch_use.add_argument("--config-root", type=Path)
    launch_use.add_argument("cli_arguments", nargs=argparse.REMAINDER)

    skill = commands.add_parser("skill", help="预览并执行 Skill 的发布、挂载、卸载、放弃修改和删除")
    skill_commands = skill.add_subparsers(dest="skill_command", required=True)
    for command in ("preview", "apply"):
        command_parser = skill_commands.add_parser(command)
        command_parser.add_argument("--library", type=Path)
        command_parser.add_argument("--config-root", type=Path)
        command_parser.add_argument("--unit", required=True)
        command_parser.add_argument("--action", required=True, choices=ACTIONS)
        if command == "apply":
            command_parser.add_argument("--token", required=True)

    web = commands.add_parser(
        "web",
        help="启动或停止本地 PAL 控制台",
    )
    web.add_argument("web_action", nargs="?", choices=["stop"], help="关闭已启动的服务")
    web.add_argument("--library", type=Path)
    web.add_argument("--config-root", type=Path)
    web.add_argument("--host", default=DEFAULT_HOST, help="本机监听地址（默认 127.0.0.1）")
    web.add_argument(
        "--port",
        default=DEFAULT_PORT,
        type=int,
        help="本次监听端口（默认 8787；0 自动选择空闲端口）",
    )
    web.add_argument("--no-browser", action="store_true", help="只显示地址，不自动打开浏览器")
    commands._choices_actions[:] = [
        action
        for action in commands._choices_actions
        if action.dest in {"quickstart", "web", "publish", "sync", "doctor"}
    ]
    return parser


def _emit(value: dict[str, object], *, stderr: bool = False) -> None:
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        file=sys.stderr if stderr else sys.stdout,
    )


def _emit_quickstart_summary(result: dict[str, object]) -> None:
    """Print the concise result intended for an interactive Quickstart user."""

    status_labels = {
        "creation-ready": "创建就绪",
        "production-ready": "生产就绪",
    }
    status = str(result["setup_status"])
    compatibility = result["compatibility"]
    if not isinstance(compatibility, dict):
        raise RuntimeError("quickstart compatibility result must be an object")

    print("")
    print("PAL 设置完成")
    print(f"状态：{status_labels.get(status, status)}")
    print(f"外挂库：{result['library_root']}")
    for cli_id, label in (("claude-code", "Claude Code"), ("codex", "Codex")):
        diagnostic = compatibility.get(cli_id)
        if not isinstance(diagnostic, dict):
            raise RuntimeError(f"quickstart compatibility result missing {cli_id}")
        print(f"{label}：检查通过（{diagnostic['actual_version']}）")
    print("配置挂载：已就绪")
    print("创建入口：已就绪")
    print("完整性检查：通过")
    if result["production_ready"]:
        print(f"生产版本：{result['active_production_version_id']}")
    print(f"下一步：{result['next_action']}")


def _emit_quickstart_failure(exc: Exception) -> None:
    """Print an actionable failure without exposing the machine proof envelope."""

    print("", file=sys.stderr)
    print("PAL 设置未完成", file=sys.stderr)
    print(f"原因：{exc}", file=sys.stderr)
    if isinstance(exc, QuickstartError):
        print(f"停止阶段：{exc.stage}", file=sys.stderr)
        if exc.completed_stages:
            print(f"已完成：{', '.join(exc.completed_stages)}", file=sys.stderr)
        print(f"下一步：{exc.next_action}", file=sys.stderr)


def _enable_interactive_line_editing() -> None:
    """Install Python's platform readline hook before using built-in input."""

    with suppress(ImportError):
        importlib.import_module("readline")


def _interactive_input(prompt: str) -> str:
    """Use built-in input's readline editor while handling a closed stream."""

    try:
        return input(prompt)
    except EOFError as exc:
        raise QuickstartCancelled("输入已结束，尚未修改任何文件。") from exc


def _read_task(path: str) -> str:
    try:
        return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise UsageError(f"production task is not valid UTF-8: {path}") from exc


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "quickstart":
            machine_output = arguments.answers is not None
            if machine_output:
                answers = load_quickstart_answers(arguments.answers)
            else:
                _enable_interactive_line_editing()
                answers = collect_quickstart_answers(
                    input_fn=_interactive_input,
                    output_fn=print,
                )
            result = run_quickstart(
                answers,
                output_fn=lambda message: print(message, file=sys.stderr),
            )
            if machine_output:
                _emit({"proof": "PAL_QUICKSTART_COMPLETED", **result})
            else:
                _emit_quickstart_summary(result)
            return 0
        if arguments.command == "init":
            result = initialize_library(arguments.library, arguments.library_id)
            _emit({"proof": "PAL_LIBRARY_INITIALIZED", **result})
            return 0
        if arguments.command == "doctor":
            result = check_library_contents(arguments.library)
            _emit({"proof": "PAL_LIBRARY_HEALTHY", **result})
            return 0
        if arguments.command == "compatibility" and arguments.compatibility_command == "check":
            result = detect_cli_compatibility(arguments.cli)
            _emit({"proof": "PAL_CLI_COMPATIBILITY_VERIFIED", **result.diagnostic()})
            return 0
        if arguments.command == "mount" and arguments.mount_command == "config":
            result = mount_config(
                arguments.library,
                arguments.cli,
                config_root=arguments.config_root,
            )
            _emit({"proof": "PAL_CONFIG_MOUNT_CREATED", **result})
            return 0
        if arguments.command == "mount" and arguments.mount_command == "production":
            library_root = resolve_library_root(
                arguments.library,
                config_root=arguments.config_root,
            )
            result = sync_production(
                library_root,
                expected_version_id=arguments.version,
                config_root=arguments.config_root,
            )
            _emit({"proof": "PAL_PRODUCTION_MOUNTED", **result})
            return 0
        if arguments.command == "create" and arguments.create_command == "inspect":
            root = resolve_library_root(arguments.library, config_root=arguments.config_root)
            result = inspect_creation(root, arguments.unit, config_root=arguments.config_root)
            _emit({"proof": "PAL_CREATION_INSPECTED", **result})
            return 0
        if arguments.command == "create" and arguments.create_command == "list":
            root = resolve_library_root(arguments.library, config_root=arguments.config_root)
            result = list_creation_units(root, config_root=arguments.config_root)
            _emit({"proof": "PAL_CREATION_UNITS_LISTED", **result})
            return 0
        if arguments.command == "create" and arguments.create_command == "begin":
            library_root = resolve_library_root(
                arguments.library,
                config_root=arguments.config_root,
            )
            result = begin_creation(
                library_root,
                arguments.cli,
                arguments.request_file,
                config_root=arguments.config_root,
            )
            _emit({"proof": "PAL_CREATION_OPENED", **result})
            return 0
        if arguments.command == "create" and arguments.create_command == "commit":
            library_root = resolve_library_root(
                arguments.library,
                config_root=arguments.config_root,
            )
            result = commit_creation(
                library_root,
                arguments.creation,
                config_root=arguments.config_root,
            )
            _emit({"proof": "PAL_CREATION_COMMITTED", **result})
            return 0
        if arguments.command == "create" and arguments.create_command == "abort":
            library_root = resolve_library_root(
                arguments.library,
                config_root=arguments.config_root,
            )
            result = abort_creation(library_root, arguments.creation)
            _emit({"proof": "PAL_CREATION_ABORTED", **result})
            return 0
        if arguments.command == "release" and arguments.release_command == "create":
            library_root = resolve_library_root(arguments.library)
            result = create_release(library_root, arguments.unit, arguments.revision)
            _emit({"proof": "PAL_RELEASE_CREATED", **result})
            return 0
        if arguments.command == "publish":
            library_root = resolve_library_root(
                arguments.library,
                config_root=arguments.config_root,
            )
            result = publish_unit(
                library_root,
                arguments.unit,
                config_root=arguments.config_root,
                sync=arguments.sync,
            )
            _emit({"proof": "PAL_UNIT_PUBLISHED", **result})
            return 0
        if arguments.command == "sync":
            root = resolve_library_root(arguments.library, config_root=arguments.config_root)
            result = sync_production(root, config_root=arguments.config_root)
            _emit({"proof": "PAL_PRODUCTION_SYNCED", **result})
            return 0
        if arguments.command == "production" and arguments.production_command == "status":
            root = resolve_library_root(arguments.library, config_root=arguments.config_root)
            status = library_status(root, resolve_config_root(arguments.config_root, create=False))
            _emit(
                {
                    "proof": "PAL_PRODUCTION_STATUS",
                    **status["library"],
                    "published_production_version_id": status["production"]["active_version_id"],
                    "active_production_version_id": status["mount"]["version_id"],
                    "sync_required": status["mount"]["sync_required"],
                    "recovery_required": status["mount"]["recovery_required"],
                    "units": status["units"],
                }
            )
            return 0
        if arguments.command == "production" and arguments.production_command == "migrate-names":
            root = resolve_library_root(arguments.library, config_root=arguments.config_root)
            result = migrate_production_namespace(
                root, config_root=arguments.config_root, sync=arguments.sync
            )
            _emit({"proof": "PAL_PRODUCTION_NAMES_MIGRATED", **result})
            return 0
        if arguments.command == "production" and arguments.production_command == "compose":
            library_root = resolve_library_root(arguments.library)
            result = compose_production(library_root, arguments.release_ids)
            _emit({"proof": "PAL_PRODUCTION_COMPOSED", **result})
            return 0
        if arguments.command == "production" and arguments.production_command == "activate":
            library_root = resolve_library_root(
                arguments.library,
                config_root=arguments.config_root,
            )
            result = sync_production(
                library_root,
                expected_version_id=arguments.version,
                config_root=arguments.config_root,
            )
            _emit({"proof": "PAL_PRODUCTION_ACTIVATED", **result})
            return 0
        if arguments.command == "skill":
            root = resolve_library_root(arguments.library, config_root=arguments.config_root)
            if arguments.skill_command == "preview":
                result = preview_skill_action(
                    root, arguments.action, arguments.unit, config_root=arguments.config_root
                )
            else:
                result = execute_skill_action(
                    root,
                    arguments.action,
                    arguments.unit,
                    arguments.token,
                    config_root=arguments.config_root,
                )
            _emit({"proof": "PAL_SKILL_ACTION", **result})
            return 0
        if arguments.command == "recover":
            library_root = resolve_library_root(
                arguments.library,
                config_root=arguments.config_root,
            )
            skill_result = recover_skill_action(library_root, config_root=arguments.config_root)
            deletion_result = recover_deletion(library_root, config_root=arguments.config_root)
            production_result = recover_production(
                library_root,
                config_root=arguments.config_root,
            )
            usage_result = recover_usage_transactions(library_root)
            _emit(
                {
                    "proof": "PAL_PRODUCTION_RECOVERED",
                    **production_result,
                    "usage_recovery": usage_result,
                    "deletion_recovery": deletion_result,
                    "skill_recovery": skill_result,
                }
            )
            return 0
        if arguments.command == "run":
            library_root = resolve_library_root(
                arguments.library,
                config_root=arguments.config_root,
            )
            result = run_production_task(
                library_root,
                arguments.cli,
                _read_task(arguments.task_file),
                config_root=arguments.config_root,
            )
            _emit({"proof": "PAL_PRODUCTION_USAGE_RECORDED", **result})
            if result["status"] == "succeeded":
                return 0
            if result["status"] == "interrupted":
                return 130
            return 1
        if arguments.command == "usage" and arguments.usage_command == "validate":
            library_root = resolve_library_root(
                arguments.library,
                config_root=arguments.config_root,
            )
            result = validate_usage_record(
                library_root,
                arguments.record,
                config_root=arguments.config_root,
            )
            _emit({"proof": "PAL_USAGE_RECORD_VALID", **result})
            return 0
        if arguments.command == "launch" and arguments.launch_command == "create":
            library_root = resolve_library_root(
                arguments.library,
                config_root=arguments.config_root,
            )
            if arguments.prepare_only:
                result = prepare_creation_adapter(
                    library_root,
                    arguments.cli,
                    config_root=arguments.config_root,
                )
                _emit({"proof": "PAL_CREATION_ADAPTER_READY", **result})
                return 0
            cli_arguments = arguments.cli_arguments
            if cli_arguments[:1] == ["--"]:
                cli_arguments = cli_arguments[1:]
            return launch_creation_entry(
                library_root,
                arguments.cli,
                config_root=arguments.config_root,
                cli_arguments=cli_arguments,
            )
        if arguments.command == "launch" and arguments.launch_command == "use":
            library_root = resolve_library_root(
                arguments.library,
                config_root=arguments.config_root,
            )
            cli_arguments = arguments.cli_arguments
            if cli_arguments[:1] == ["--"]:
                cli_arguments = cli_arguments[1:]
            return launch_production_entry(
                library_root,
                arguments.cli,
                config_root=arguments.config_root,
                cli_arguments=cli_arguments,
            )
        if arguments.command == "web":
            if arguments.web_action == "stop":
                return stop_web(config_root=arguments.config_root, port=arguments.port)
            library_root = resolve_library_root(
                arguments.library,
                config_root=arguments.config_root,
            )
            return run_web(
                library_root,
                config_root=arguments.config_root,
                host=arguments.host,
                port=arguments.port,
                open_browser=not arguments.no_browser,
            )
    except QuickstartCancelled as exc:
        if arguments.command == "quickstart" and arguments.answers is None:
            print(f"\n已取消：{exc}", file=sys.stderr)
        else:
            _emit({"proof": "PAL_QUICKSTART_CANCELLED", "message": str(exc)}, stderr=True)
        return 0
    except (PALError, OSError) as exc:
        if arguments.command == "quickstart":
            proof = "PAL_QUICKSTART_REJECTED"
        elif arguments.command == "init":
            proof = "PAL_LIBRARY_INIT_REJECTED"
        elif arguments.command == "doctor":
            proof = "PAL_LIBRARY_DOCTOR_REJECTED"
        elif arguments.command == "compatibility":
            proof = "PAL_CLI_COMPATIBILITY_REJECTED"
        elif arguments.command == "mount":
            proof = (
                "PAL_CONFIG_MOUNT_REJECTED"
                if arguments.mount_command == "config"
                else "PAL_PRODUCTION_MOUNT_REJECTED"
            )
        elif arguments.command == "create":
            proof = f"PAL_CREATION_{arguments.create_command.upper()}_REJECTED"
        elif arguments.command == "release":
            proof = "PAL_RELEASE_CREATE_REJECTED"
        elif arguments.command == "publish":
            proof = "PAL_PUBLISH_REJECTED"
        elif arguments.command == "sync":
            proof = "PAL_PRODUCTION_SYNC_REJECTED"
        elif arguments.command == "production":
            proof = f"PAL_PRODUCTION_{arguments.production_command.upper()}_REJECTED"
        elif arguments.command == "recover":
            proof = "PAL_PRODUCTION_RECOVERY_REJECTED"
        elif arguments.command == "run":
            proof = "PAL_PRODUCTION_RUN_REJECTED"
        elif arguments.command == "usage":
            proof = "PAL_USAGE_RECORD_REJECTED"
        elif arguments.command == "launch" and arguments.launch_command == "use":
            proof = "PAL_PRODUCTION_LAUNCH_REJECTED"
        elif arguments.command == "web":
            proof = "PAL_WEB_REJECTED"
        else:
            proof = "PAL_CREATION_LAUNCH_REJECTED"
        rejection: dict[str, object] = {"proof": proof, "error": str(exc)}
        if isinstance(exc, QuickstartError):
            rejection.update(
                {
                    "stage": exc.stage,
                    "completed_stages": list(exc.completed_stages),
                    "next_action": exc.next_action,
                }
            )
        if arguments.command == "quickstart" and arguments.answers is None:
            _emit_quickstart_failure(exc)
        elif arguments.command == "web":
            print(f"PAL Web 未启动：{exc}", file=sys.stderr)
        else:
            _emit(rejection, stderr=True)
        return REJECTION_EXIT_CODE
    raise RuntimeError(f"unhandled PAL command: {arguments.command}")
