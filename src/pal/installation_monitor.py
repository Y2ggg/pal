"""Read official installation inventories and repair explicitly selected PAL copies.

Traceability: MON-001 through MON-006; PRD-MOUNT-001/003/004;
PRD-CLI-002; ACC-008, ACC-011, ACC-012.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import __version__, adapters
from . import production_mount as mounts
from .config_mount import resolve_config_root
from .errors import IntegrityError, PALError, ProductionError
from .library import load_json_object, utc_now
from .maintenance import lifecycle_write
from .paths import canonical_existing_root, validate_regular_tree
from .platform_support import command_for_platform
from .publishing import library_lock
from .schema_catalog import TARGET_CLIS
from .stages import current_production_id
from .targets import target_driver

READ_TIMEOUT = 8
REPAIRABLE = {"missing", "outdated", "disabled", "drift"}


def _result(state: str, message: str, **fields: Any) -> dict[str, Any]:
    return {"state": state, "message": message, "repairable": state in REPAIRABLE, **fields}


def _inventory(cli_id: str, executable: str, *, marketplaces: bool = False) -> list[dict]:
    args = [executable, "plugin", *(["marketplace"] if marketplaces else []), "list", "--json"]
    try:
        process = subprocess.run(
            command_for_platform(args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=READ_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProductionError(f"{cli_id} 安装清单读取超时，请稍后重新检查") from exc
    if process.returncode:
        raise ProductionError(f"{cli_id} 安装清单读取失败：{process.stderr.strip()}")
    payload = json.loads(process.stdout)
    if cli_id == "codex":
        if not isinstance(payload, dict):
            raise ProductionError("Codex 安装清单响应无效")
        payload = payload.get("marketplaces" if marketplaces else "installed")
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ProductionError(f"{cli_id} 安装清单响应无效")
    return payload


def _plugin_id(cli_id: str, entry: dict) -> Any:
    return entry.get("pluginId" if cli_id == "codex" else "id")


def _installed(cli_id: str, entries: list[dict]) -> list[dict]:
    return [item for item in entries if cli_id != "codex" or item.get("installed") is True]


def _cache_root(marketplace: str, plugin: str, version: str) -> Path:
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    if (
        not home.is_absolute()
        or not version
        or Path(version).name != version
        or version in {".", ".."}
    ):
        raise IntegrityError("Codex 缓存身份无效")
    return home / "plugins/cache" / marketplace / plugin / version


def _entry_root(cli_id: str, entry: dict) -> Path:
    source = entry.get("source")
    value = (
        source.get("path")
        if cli_id == "codex" and isinstance(source, dict)
        else entry.get("installPath")
    )
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise IntegrityError("官方清单未提供有效的插件路径")
    return canonical_existing_root(Path(value))


def _matching(cli_id: str, entries: list[dict], plugin: str, marketplace: str) -> tuple[list, list]:
    selector = f"{plugin}@{marketplace}"
    entries = _installed(cli_id, entries)
    matching = [item for item in entries if _plugin_id(cli_id, item) == selector]
    foreign = [
        item
        for item in entries
        if str(_plugin_id(cli_id, item)).startswith(f"{plugin}@")
        and _plugin_id(cli_id, item) != selector
    ]
    return matching, foreign


def _marketplace_root(cli_id: str, entries: list[dict], name: str) -> Path | None:
    matching = [item for item in entries if item.get("name") == name]
    if not matching:
        return None
    if len(matching) != 1:
        raise IntegrityError("同名 marketplace 不唯一")
    value = matching[0].get("root" if cli_id == "codex" else "path")
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise IntegrityError("marketplace 路径无效")
    return Path(value).resolve()


def _creation_status(cli_id: str, config: Path, plugins: list[dict], markets: list[dict]) -> dict:
    version = f"{__version__}+{adapters.CREATION_ADAPTER_SUFFIX}"
    matching = [
        item
        for item in _installed(cli_id, plugins)
        if str(_plugin_id(cli_id, item)).startswith(f"{adapters.PLUGIN_NAME}@")
    ]
    installed_marketplace = (
        str(_plugin_id(cli_id, matching[0])).split("@", 1)[1] if len(matching) == 1 else None
    )
    marketplace = installed_marketplace or adapters._marketplace_name()
    fields = {
        "expected_version": version,
        "installed_version": matching[0].get("version") if len(matching) == 1 else None,
        "installed_marketplace": installed_marketplace,
    }
    if len(matching) > 1:
        return _result("conflict", "存在同名或重复系统入口，请核对官方插件清单。", **fields)
    try:
        adapters.validate_creation_source(cli_id, config)
    except (PALError, OSError) as exc:
        return _result("source-drift", str(exc), **fields)
    market = _marketplace_root(cli_id, markets, marketplace)
    if market is not None:
        # Only this PAL config root's versioned creation layout is owned here.
        relative = (
            market.relative_to(config / "targets/creation")
            if market.is_relative_to(config / "targets/creation")
            else None
        )
        expected_parts = 4 if cli_id == "codex" else 3
        if (
            relative is None
            or len(relative.parts) != expected_parts
            or relative.parts[2] != cli_id
            or marketplace
            != "pal-" + re.sub(r"[^a-z0-9]+", "-", relative.parts[1].lower()).strip("-")
            or not adapters._managed_creation_marketplace(market, cli_id, marketplace)
        ):
            return _result("conflict", "系统入口来源不属于当前 PAL 配置，拒绝覆盖。", **fields)
    if not matching:
        return _result("missing", "系统创建入口尚未安装。", **fields)
    if market is None:
        return _result("conflict", "已安装入口的来源未登记，需先核对官方 marketplace。", **fields)
    entry = matching[0]
    if cli_id == "codex":
        source = entry.get("source", {})
        if not isinstance(source, dict) or source.get("path") != str(
            market / "plugins" / adapters.PLUGIN_NAME
        ):
            return _result("conflict", "已安装系统入口与登记来源不一致。", **fields)
    if entry.get("version") != version or marketplace != adapters._marketplace_name():
        return _result("outdated", "系统创建入口与当前 PAL 要求的版本不同。", **fields)
    if entry.get("enabled") is not True:
        return _result("disabled", "系统创建入口未启用。", **fields)
    try:
        relative, expected = adapters._projection_files(cli_id, config)
        expected = {
            path.removeprefix(relative + "/"): value
            for path, value in expected.items()
            if path.startswith(relative + "/")
        }
        mounts._verify_files(_entry_root(cli_id, entry), expected, "系统创建入口")
        if cli_id == "codex":
            mounts._verify_files(
                _cache_root(adapters._marketplace_name(), adapters.PLUGIN_NAME, version),
                expected,
                "Codex 系统入口缓存",
            )
    except (PALError, OSError) as exc:
        return _result("drift", f"系统入口文件校验失败：{exc}", **fields)
    return _result("healthy", "系统创建入口版本、启用状态和文件校验通过。", **fields)


def _mount_status(
    cli_id: str, plan: mounts.TargetPlan | None, plugins: list[dict], markets: list[dict]
) -> dict:
    if plan is None:
        return _result("unconfigured", "尚未向 CLI 同步生产内容。")
    matching, foreign = _matching(cli_id, plugins, plan.plugin_name, plan.marketplace_name)
    legacy_prefix = f"{mounts.PRODUCTION_PLUGIN_PREFIX}-{mounts._slug(plan.library_id)}-"
    if any(
        str(_plugin_id(cli_id, item)).startswith(legacy_prefix)
        and _plugin_id(cli_id, item) != mounts._target_plugin_id(plan, cli_id)
        for item in _installed(cli_id, plugins)
    ):
        foreign.append({})
    if foreign or len(matching) > 1:
        return _result("conflict", "存在其他来源或旧版 PAL 挂载，请先核对插件清单。")
    expected_market = (
        mounts._marketplace_root(plan) if cli_id == "codex" else plan.install_roots[cli_id]
    )
    actual_market = _marketplace_root(cli_id, markets, plan.marketplace_name)
    if actual_market is not None and actual_market != expected_market.resolve():
        return _result("conflict", "挂载 marketplace 指向其他来源，拒绝覆盖。")
    if not matching:
        return _result("missing", "PAL 有同步记录，但 CLI 中未安装对应插件。")
    if actual_market is None:
        return _result("conflict", "已安装挂载的来源未登记，需先核对官方 marketplace。")
    entry = matching[0]
    if cli_id == "codex" and (
        not isinstance(entry.get("source"), dict)
        or entry["source"].get("path") != str(mounts._plugin_root(plan, cli_id))
    ):
        return _result("conflict", "CLI 挂载源路径与 PAL 投影不一致。")
    if entry.get("enabled") is not True:
        return _result("disabled", "CLI 中的业务 Skill 插件未启用。")
    version = mounts._production_plugin_version(plan.validated, cli_id)
    if entry.get("version") != version:
        return _result("drift", "CLI 实际安装版本与上次同步记录不一致。")
    try:
        actual = _entry_root(cli_id, entry)
        validate_regular_tree(actual)
        mounts._verify_installed_tree(actual, mounts._plugin_root(plan, cli_id))
        if cli_id == "codex":
            mounts._codex_cached_plugin_root(plan, entry)
    except (PALError, OSError) as exc:
        return _result("drift", f"挂载文件校验失败：{exc}")
    return _result("healthy", "已同步内容的安装、启用状态和文件校验通过。")


def _read_plan(root: Path, config: Path, library_id: str) -> tuple[dict, mounts.TargetPlan | None]:
    active, _ = mounts._read_active(root, library_id)
    if active["active_production_version_id"] is None:
        return active, None
    plan = mounts._build_target_plan(
        root,
        config,
        library_id,
        active["active_production_version_id"],
        activation_time=active["activated_at"],
        executables={},
        create_layout=False,
    )
    if plan.active != active:
        raise IntegrityError("CLI 挂载指针与不可变内容不一致")
    mounts.validate_production_mount(root, plan.version_id, config_root=config)
    return active, plan


def _inspect_target(
    cli_id: str, config: Path, plan: mounts.TargetPlan | None, mount_error: str | None
) -> dict:
    result = {"cli_id": cli_id}
    try:
        executable = shutil.which(target_driver(cli_id).executable_name)
        if not executable:
            raise ProductionError("未找到 CLI 可执行文件，请先安装 CLI。")
        plugins = _inventory(cli_id, executable)
        markets = _inventory(cli_id, executable, marketplaces=True)
        for kind in ("creation", "mount"):
            try:
                result[kind] = (
                    _creation_status(cli_id, config, plugins, markets)
                    if kind == "creation"
                    else _result("recovery-required", mount_error)
                    if mount_error
                    else _mount_status(cli_id, plan, plugins, markets)
                )
            except (PALError, OSError, ValueError) as exc:
                result[kind] = _result("unknown", f"检查未完成：{exc}")
    except (PALError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result.update(
            {kind: _result("unknown", f"检查未完成：{exc}") for kind in ("creation", "mount")}
        )
    return result


@lifecycle_write
def inspect_installations(root: Path, *, config_root: Path) -> dict:
    root = canonical_existing_root(root)
    config = resolve_config_root(config_root, create=False)
    library_id = load_json_object(root / "library.json")["library_id"]
    with library_lock(root, "publication", "安装检查"), mounts._activation_lock(config, library_id):
        active, _ = mounts._read_active(root, library_id)
        published = current_production_id(root)
        plan, error = None, None
        try:
            _, plan = _read_plan(root, config, library_id)
            if mounts._transition_path(config, library_id).exists():
                error = "CLI 同步事务未完成，请先执行异常恢复。"
        except (PALError, OSError, ValueError) as exc:
            error = f"PAL 挂载材料校验失败：{exc}"
        with ThreadPoolExecutor(max_workers=2) as executor:
            targets = list(
                executor.map(lambda cli: _inspect_target(cli, config, plan, error), TARGET_CLIS)
            )
        return {
            "checked_at": utc_now(),
            "pal_version": __version__,
            "production_version_id": published,
            "mounted_version_id": active["active_production_version_id"],
            "targets": targets,
        }


def repair_installation(
    root: Path, *, config_root: Path, cli_id: str, kind: str, expected_version: str
) -> dict:
    if cli_id not in TARGET_CLIS or kind not in {"creation", "mount"}:
        raise ProductionError("修复对象无效")
    root = canonical_existing_root(root)
    config = resolve_config_root(config_root, create=False)
    if kind == "creation":
        if expected_version != f"{__version__}+{adapters.CREATION_ADAPTER_SUFFIX}":
            raise ProductionError("PAL 系统入口版本已变化，请刷新后重新确认")
        adapters.validate_creation_source(cli_id, config)
        # The installer takes a config-wide lock shared with Quickstart.
        adapters.install_creation_adapter(root, cli_id, config_root=config, repair=True)
    else:
        _repair_mount(root, config, cli_id, expected_version)
    report = inspect_installations(root, config_root=config)
    target = next(item for item in report["targets"] if item["cli_id"] == cli_id)
    if target[kind]["state"] != "healthy":
        raise ProductionError(f"操作后核验未通过：{target[kind]['message']}")
    return {
        "proof": "PAL_INSTALLATION_REPAIRED",
        "monitor": report,
        "message": "修复并核验完成，请打开新 CLI 会话使用。",
    }


@lifecycle_write
def _repair_mount(root: Path, config: Path, cli_id: str, expected_version: str) -> None:
    library_id = load_json_object(root / "library.json")["library_id"]
    with (
        library_lock(root, "publication", "修复 CLI 挂载"),
        mounts._activation_lock(config, library_id),
    ):
        if mounts._transition_path(config, library_id).exists():
            raise ProductionError("存在未完成同步，请先执行异常恢复")
        active, plan = _read_plan(root, config, library_id)
        if plan is None or active["active_production_version_id"] != expected_version:
            raise ProductionError("CLI 挂载内容已变化，请刷新后重新确认")
        executable = adapters.require_compatible_cli(cli_id).executable
        state = _mount_status(
            cli_id,
            plan,
            _inventory(cli_id, executable),
            _inventory(cli_id, executable, marketplaces=True),
        )
        if state["state"] == "healthy":
            return
        if not state["repairable"]:
            raise ProductionError(state["message"])
        plan.executables[cli_id] = executable
        mounts._remove_target(plan, cli_id)
        mounts._install_target(plan, cli_id)
        mounts._verify_installed(plan, cli_id)
