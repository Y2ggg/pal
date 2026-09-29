"""Versioned Claude Code and Codex creation-entry projections.

Adapter projections are reproducible PAL configuration state.  They contain a
shared semantic ``pal-create-skill`` Skill and CLI-directed discovery shells;
they never become the development-library fact source.

Traceability: PRD-CLI-001 through PRD-CLI-003, PRD-MOUNT-001,
PRD-CREATE-001; ACC-003, ACC-004, ACC-012.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .compatibility import CliCompatibility, detect_cli_compatibility
from .config_mount import resolve_config_root, resolve_creation_context
from .errors import CreationError, IntegrityError, PALError, PathSafetyError
from .io import fsync_directory, fsync_tree, tree_digest, write_new_bytes
from .paths import canonical_existing_root, require_inside, validate_regular_tree
from .platform_support import command_for_platform, is_link, move_path, shell_quote
from .schema_catalog import TARGET_CLIS
from .targets import target_driver

PLUGIN_NAME = "pal"
SKILL_NAME = "pal-create-skill"
CREATION_LAYOUT_VERSION = "native-v8"
CREATION_ADAPTER_SUFFIX = "native.8"

CREATE_SKILL_TEMPLATE = """---
name: pal-create-skill
description: Create, update, publish and mount PAL Skills for Claude Code and Codex.
---

# PAL cross-CLI Skill creation

Before starting a transaction, classify the request:

- For a new Skill, obtain a concrete `unit_id` from the user's requested name.
- For an update or refactor, the user must identify the exact existing Skill and describe the
  requested changes. The PAL library ID (for example `agent-abilities-store`) is not a Skill
  `unit_id` and must never be passed to `pal create inspect`.
- If either the target Skill or the requested change is missing, do not run `inspect`, `begin`,
  or `commit`. You may run the read-only command
  `pal create list --config-root {config_root}` to show candidates, then ask one concise question:
  “要更新哪个 Skill（从候选中选择）？具体修改什么内容？”

Do not infer a target from the current working directory, the library name, or a previous session.

Create or update a Skill only through the PAL transaction protocol. Do not invoke a CLI built-in
creator and do not write a formal result into a CLI-private Skill directory.

1. Create a private system temporary directory. On POSIX use `mktemp -d`; on Windows PowerShell,
   use a new GUID-named directory under `[System.IO.Path]::GetTempPath()`. Write request and Skill
   text as UTF-8 without BOM (PowerShell 5: `[System.Text.UTF8Encoding]::new($false)`). Use shell-
   appropriate quoting: the config-root examples below use this machine's default shell convention
   (PowerShell on Windows, POSIX shell elsewhere). Convert the user's
   intent into a JSON request file there with exactly these fields: `schema_version`, `unit_id`,
   `summary`, and `profile_by_cli` for a new Skill. `schema_version` must be the JSON number `1`;
   a string value is invalid. For an update, use the same four fields plus `base_revision_id` and
   set `schema_version` to the JSON number `2`, as described below. Never write request or helper
   files into this Skill's base directory, any plugin directory, or an PAL library.
2. Set `profile_by_cli` keys in this order: `claude-code`, then `codex`. Use
   `skill-md-v1-basic` for both unless the request explicitly needs Claude's native `model`
   declaration field; in that case use `skill-md-v1-claude-model-pinned` for Claude and
   `skill-md-v1-basic` for Codex. Do not invent another profile.
3. Run `pal create begin --cli {cli_id} --request-file <request.json> --config-root {config_root}`.
4. Read the returned candidate list and the mounted specification/design-rule references. Write
   the required `SKILL.md` at each returned `skill_path`. Create any supporting references,
   scripts, templates, or binary assets under that candidate's `skill_root` (for example in
   `references/`, `scripts/`, or `assets/`). Keep all bundled files inside that root; use relative
   references from `SKILL.md` and resolve script resources relative to the script, not the working
   directory. Invoke scripts through their interpreter (for example `python3` or `sh`); executable
   permission bits are not preserved by the payload format. Do not use symbolic links or special
   files. Check that all referenced local resources exist. For directed candidates, include each
   candidate's complete dependencies inside its own root. Do not change artifact IDs, profile IDs,
   coverage, the transaction record, or the plan.
5. Run `pal create commit --creation <creation_id> --config-root {config_root}`, then remove only
   the private temporary directory created in step 1. Report development success only when commit
   returns `PAL_CREATION_COMMITTED`.
6. A create/update request alone authorizes only the development commit. Report that state.
   Publication and CLI mounting are separate manual actions; follow the management section below
   only for actions the user explicitly requested. An authorization already given still applies.

The basic profile permits exactly `name` and `description` frontmatter. The Claude model-pinned
profile additionally requires `model`; PAL preserves that field but does not guarantee the model
used by the target CLI. Every instruction body must be non-empty Markdown.

## Updating or refactoring an existing Skill

Use the same unit_id. First run `pal create inspect --unit <unit_id> --config-root {config_root}`.
Read the current revision and complete artifact roots. Use schema_version 2 (a JSON number), the
same four request fields plus base_revision_id set to that revision_id. Preserve profile_by_cli
unless the user requests an adaptation change. Begin copies compatible artifacts into candidates;
preserve their supporting files. If profile regrouping creates empty candidates, provide complete
variants. A stale base is rejected: inspect again and reapply the changes in a new transaction.
After an interrupted commit, retry the same creation_id. Never edit committed files or journals.

## Publishing and managing an existing Skill

A management-only request does not start a creation or update transaction. Resolve the exact Skill
from the current conversation or `pal production status --config-root {config_root}`. The returned
units include development, production and mounted revisions plus mount_enabled and pending_deletion.
Do not require the user to supply internal IDs if their selected Skill is already unambiguous.

For one Skill, select the requested action:

- publish: save current development content to production; mounted content stays unchanged.
- mount: install the selected production Skill or its update in both CLIs; preserve other Skills.
  An explicit request to remount a stopped Skill authorizes reenabling it.
- unmount: stop mounting this Skill in both CLIs; retain development and production content.
- discard: restore development to the current production content; keep production and mounts.
- delete: remove development and current production content. If still mounted, it remains marked
  pending_deletion until explicit unmount. Explain this before executing; do not infer unmount
  authorization from a request to delete development content or to stop using a Skill.

Preview with `pal skill preview --action <action> --unit <unit_id> --config-root {config_root}`.
Explain the effect and use its token with
`pal skill apply --action <action> --unit <unit_id> --token <token> --config-root {config_root}`.
An explicit user request authorizes that action: do not ask for the same approval again. If only
exploration was requested, show the preview and wait. If a token is stale, obtain a fresh preview;
continue only within the same authorized action and scope. On other failures, report the actual
state and do not run unrelated recovery or change another Skill.

With explicit intent to publish AND synchronize this Skill, you may use
`pal publish --unit <unit_id> --sync --config-root {config_root}`. It only advances this Skill and
respects the existing mounting choice. If mount_enabled is false, report that it remains stopped.
Only an explicit remount request authorizes a separate mount action.

`pal sync --config-root {config_root}` applies ALL enabled publications and pending removals.
Use it only when the user explicitly requests the whole library. A request to synchronize one Skill
uses its mount action. Do not use production remove, snapshot activation, or rollback commands.

Report success from the command result and `pal production status --config-root {config_root}`.
PAL_CREATION_COMMITTED means development saved; PAL_UNIT_PUBLISHED means published, not installed.
CLI availability requires a successful mount/sync and a new CLI session. A sync failure does not
undo publication. Do not claim a stopped Skill is usable or treat an intentional unmount as pending.
"""


def _creation_skill(cli_id: str, config_root: Path) -> bytes:
    if cli_id not in TARGET_CLIS:
        raise CreationError(f"unsupported adapter CLI: {cli_id}")
    return CREATE_SKILL_TEMPLATE.format(
        cli_id=cli_id,
        config_root=shell_quote(str(config_root)),
    ).encode("utf-8")


def _marketplace_name() -> str:
    normalized_version = re.sub(r"[^a-z0-9]+", "-", __version__.lower()).strip("-")
    return f"pal-{normalized_version}"


def _projection_files(cli_id: str, config_root: Path) -> tuple[str, dict[str, bytes]]:
    if cli_id not in TARGET_CLIS:
        raise CreationError(f"unsupported adapter CLI: {cli_id}")
    marketplace_name = _marketplace_name()
    driver = target_driver(cli_id)
    plugin_relative, files = driver.project_plugin(
        plugin_name=PLUGIN_NAME,
        marketplace_name=marketplace_name if cli_id == "codex" else None,
        plugin_version=f"{__version__}+{CREATION_ADAPTER_SUFFIX}",
        description="PAL cross-CLI Skill creation entry",
        marketplace_display_name="PAL",
        display_name="PAL",
        short_description="Create one Skill for Claude Code and Codex.",
        long_description=(
            "Uses mounted PAL specifications and a staged transaction to create a "
            "cross-CLI Skill in the development library."
        ),
        default_prompt="Use $pal-create-skill to create a cross-CLI Skill.",
        payload_files={f"skills/{SKILL_NAME}/SKILL.md": _creation_skill(cli_id, config_root)},
    )
    if cli_id == "claude-code":
        files = driver.project_install_root(
            plugin_name=PLUGIN_NAME,
            marketplace_name=marketplace_name,
            description="PAL cross-CLI Skill creation entry",
            projection_files=files,
        )
    return plugin_relative, files


def _mkdir_descendant(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PathSafetyError(f"adapter path escapes PAL config root: {path}") from exc
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.exists() or is_link(cursor):
            if is_link(cursor) or not cursor.is_dir():
                raise PathSafetyError(f"adapter path is not a real directory: {cursor}")
        else:
            cursor.mkdir()


def _verify_projection(root: Path, expected: dict[str, bytes]) -> None:
    if is_link(root) or not root.is_dir():
        raise PathSafetyError(f"adapter projection is not a real directory: {root}")
    validate_regular_tree(root)
    actual_files = {
        path.relative_to(root).as_posix(): path for path in root.rglob("*") if path.is_file()
    }
    if set(actual_files) != set(expected):
        raise IntegrityError("adapter projection file inventory has drifted")
    for relative, material in expected.items():
        if actual_files[relative].read_bytes() != material:
            raise IntegrityError(f"adapter projection file has drifted: {relative}")


def validate_creation_source(cli_id: str, config: Path) -> None:
    """Reject existing damaged source before offering installation-cache repair.

    Traceability: MON-002/003/005; ACC-012. Never materialize or rewrite a source.
    """
    root = config / "targets/creation" / CREATION_LAYOUT_VERSION / __version__ / cli_id
    try:
        require_inside(
            config, root.relative_to(config).as_posix(), "系统入口源目录", must_exist=False
        )
        if root.exists():
            _, expected = _projection_files(cli_id, config)
            _verify_projection(root, expected)
    except (PALError, OSError) as exc:
        raise IntegrityError(
            f"系统入口源文件异常：{root}。请从同版本备份恢复该目录，"
            f"或安装新版 PAL 后更新系统入口；自动修复不可用。原因：{exc}"
        ) from exc


def _safe_remove_staging(path: Path, parent: Path) -> None:
    try:
        path.relative_to(parent)
    except ValueError as exc:  # pragma: no cover - defensive invariant
        raise CreationError(f"refusing to clean adapter staging path: {path}") from exc
    if is_link(path) or not path.name.startswith(".adapter-"):
        raise CreationError(f"refusing to clean unexpected adapter path: {path}")
    shutil.rmtree(path)


def prepare_creation_adapter(
    library_root: Path,
    cli_id: str,
    *,
    config_root: Path | None = None,
) -> dict[str, Any]:
    """Materialize or verify one immutable creation-entry projection."""

    if cli_id not in TARGET_CLIS:
        raise CreationError(f"unsupported adapter CLI: {cli_id}")
    root = canonical_existing_root(library_root)
    context = resolve_creation_context(root, config_root=config_root)
    pal_config_root = resolve_config_root(config_root, create=True)
    version_parent = (
        pal_config_root / "targets" / "creation" / CREATION_LAYOUT_VERSION / __version__
    )
    _mkdir_descendant(version_parent, pal_config_root)
    projection_root = version_parent / cli_id
    plugin_relative, expected = _projection_files(cli_id, pal_config_root)

    if projection_root.exists() or is_link(projection_root):
        validate_creation_source(cli_id, pal_config_root)
        idempotent = True
    else:
        staging = version_parent / f".adapter-{cli_id}-{secrets.token_hex(8)}.tmp"
        committed = False
        try:
            staging.mkdir()
            for relative, material in expected.items():
                write_new_bytes(staging / relative, material)
            _verify_projection(staging, expected)
            fsync_tree(staging)
            move_path(staging, projection_root)
            fsync_directory(version_parent)
            committed = True
        finally:
            if not committed and staging.exists():
                _safe_remove_staging(staging, version_parent)
        idempotent = False

    result = {
        "library_id": context["library_id"],
        "cli_id": cli_id,
        "adapter_version": __version__,
        "plugin_root": str(projection_root / plugin_relative),
        "tree_sha256": tree_digest(projection_root),
        "minimum_cli_version": target_driver(cli_id).minimum_version,
        "verified_cli_versions": sorted(target_driver(cli_id).verified_versions),
        "idempotent": idempotent,
    }
    marketplace_root = projection_root / ("marketplace" if cli_id == "codex" else "")
    marketplace_manifest = marketplace_root / (
        ".agents/plugins/marketplace.json"
        if cli_id == "codex"
        else ".claude-plugin/marketplace.json"
    )
    result.update(
        {
            "marketplace_name": _marketplace_name(),
            "marketplace_root": str(marketplace_root),
            "marketplace_manifest": str(marketplace_manifest),
        }
    )
    return result


def require_compatible_cli(cli_id: str) -> CliCompatibility:
    """Return fail-closed version and probe evidence for one installed target."""

    if cli_id not in TARGET_CLIS:
        raise CreationError(f"unsupported adapter CLI: {cli_id}")
    return detect_cli_compatibility(cli_id)


def require_supported_cli_version(cli_id: str) -> str:
    """Compatibility alias returning the admitted executable name.

    The name is retained for callers of PAL 0.1.0.  Admission is no longer an
    exact-version comparison; :func:`require_compatible_cli` owns that policy.
    """

    return require_compatible_cli(cli_id).executable


def _run_checked(arguments: list[str]) -> None:
    process = subprocess.run(
        command_for_platform(arguments),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip()
        raise CreationError(f"adapter command failed ({' '.join(arguments[:4])}): {detail}")


def _managed_creation_marketplace(
    root: Path, cli_id: str, marketplace_name: str | None = None
) -> bool:
    """Recognize only PAL creation marketplaces before replacing a stale source."""

    try:
        resolved = root.resolve(strict=True)
    except OSError:
        return False
    if "targets" not in resolved.parts or "creation" not in resolved.parts:
        return False
    if cli_id == "codex":
        manifest = resolved / ".agents/plugins/marketplace.json"
        skill = resolved / f"plugins/{PLUGIN_NAME}/skills/{SKILL_NAME}/SKILL.md"
    else:
        manifest = resolved / ".claude-plugin/marketplace.json"
        skill = resolved / f"plugins/{PLUGIN_NAME}/skills/{SKILL_NAME}/SKILL.md"
    if not manifest.is_file() or not skill.is_file():
        return False
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return payload.get("name") == (marketplace_name or _marketplace_name()) and skill.read_text(
        encoding="utf-8"
    ).startswith("---\nname: pal-create-skill\n")


def _codex_plugins(executable: str) -> list[dict[str, Any]]:
    installed = subprocess.run(
        command_for_platform([executable, "plugin", "list", "--json"]),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if installed.returncode != 0:
        raise CreationError(f"cannot verify installed Codex plugin: {installed.stderr.strip()}")
    try:
        entries = json.loads(installed.stdout)["installed"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CreationError("Codex plugin list returned an invalid JSON envelope") from exc
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise CreationError("Codex plugin list returned invalid installed entries")
    return entries


def _ensure_codex_projection(executable: str, adapter: dict[str, Any]) -> None:
    listed = subprocess.run(
        command_for_platform([executable, "plugin", "marketplace", "list", "--json"]),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if listed.returncode != 0:
        raise CreationError(f"cannot list Codex marketplaces: {listed.stderr.strip()}")
    try:
        payload = json.loads(listed.stdout)
        marketplaces = payload["marketplaces"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CreationError("Codex marketplace list returned an invalid JSON envelope") from exc
    if not isinstance(marketplaces, list) or not all(
        isinstance(item, dict) for item in marketplaces
    ):
        raise CreationError("Codex marketplace list returned invalid entries")
    matching = [item for item in marketplaces if item.get("name") == adapter["marketplace_name"]]
    if matching:
        actual_root = Path(matching[0].get("root", "")).resolve(strict=False)
        expected_root = Path(adapter["marketplace_root"]).resolve(strict=True)
        if actual_root != expected_root:
            if not _managed_creation_marketplace(actual_root, "codex"):
                raise CreationError(
                    "Codex marketplace name already points elsewhere: "
                    f"{adapter['marketplace_name']}"
                )
            plugin_id = f"{PLUGIN_NAME}@{adapter['marketplace_name']}"
            if any(
                item.get("pluginId") == plugin_id and item.get("installed")
                for item in _codex_plugins(executable)
            ):
                _run_checked([executable, "plugin", "remove", plugin_id, "--json"])
            _run_checked(
                [
                    executable,
                    "plugin",
                    "marketplace",
                    "remove",
                    adapter["marketplace_name"],
                    "--json",
                ]
            )
            matching = []
    if not matching:
        _run_checked(
            [
                executable,
                "plugin",
                "marketplace",
                "add",
                adapter["marketplace_root"],
                "--json",
            ]
        )
    _run_checked(
        [
            executable,
            "plugin",
            "add",
            f"{PLUGIN_NAME}@{adapter['marketplace_name']}",
            "--json",
        ]
    )
    installed_entries = _codex_plugins(executable)
    expected_plugin_id = f"{PLUGIN_NAME}@{adapter['marketplace_name']}"
    matching_plugins = [
        item
        for item in installed_entries
        if isinstance(item, dict) and item.get("pluginId") == expected_plugin_id
    ]
    if len(matching_plugins) != 1 or not matching_plugins[0].get("installed"):
        raise CreationError(f"Codex did not install the expected plugin: {expected_plugin_id}")
    if matching_plugins[0].get("enabled") is not True:
        raise CreationError("Codex 系统创建入口未启用，请在 Codex 中启用后重新检查")
    installed_root = Path(matching_plugins[0].get("source", {}).get("path", ""))
    installed_skill = installed_root / f"skills/{SKILL_NAME}/SKILL.md"
    expected_skill = Path(adapter["plugin_root"]) / f"skills/{SKILL_NAME}/SKILL.md"
    if not installed_skill.is_file() or installed_skill.read_bytes() != expected_skill.read_bytes():
        raise IntegrityError("installed Codex creation Skill does not match the PAL projection")


def _claude_json_array(arguments: list[str], label: str) -> list[dict[str, Any]]:
    process = subprocess.run(
        command_for_platform(arguments),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if process.returncode != 0:
        raise CreationError(f"{label} failed: {process.stderr.strip() or process.stdout.strip()}")
    try:
        entries = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise CreationError(f"{label} returned invalid JSON") from exc
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise CreationError(f"{label} returned invalid entries")
    return entries


def _ensure_claude_projection(executable: str, adapter: dict[str, Any]) -> None:
    marketplace_name = adapter["marketplace_name"]
    selector = f"{PLUGIN_NAME}@{marketplace_name}"
    marketplaces = _claude_json_array(
        command_for_platform([executable, "plugin", "marketplace", "list", "--json"]),
        "Claude marketplace list",
    )
    matching = [item for item in marketplaces if item.get("name") == marketplace_name]
    if len(matching) > 1:
        raise CreationError(f"Claude marketplace is duplicated: {marketplace_name}")
    expected_root = Path(adapter["marketplace_root"]).resolve(strict=True)
    if matching:
        actual_root = Path(str(matching[0].get("path", ""))).resolve(strict=False)
        if actual_root != expected_root:
            if not _managed_creation_marketplace(actual_root, "claude-code"):
                raise CreationError(
                    f"Claude marketplace name already points elsewhere: {marketplace_name}"
                )
            plugins = _claude_json_array(
                command_for_platform([executable, "plugin", "list", "--json"]),
                "Claude plugin list",
            )
            if any(item.get("id") == selector for item in plugins):
                _run_checked([executable, "plugin", "uninstall", selector])
            _run_checked([executable, "plugin", "marketplace", "remove", marketplace_name])
            matching = []
    if not matching:
        _run_checked(
            [
                executable,
                "plugin",
                "marketplace",
                "add",
                str(expected_root),
                "--scope",
                "user",
            ]
        )
    _run_checked([executable, "plugin", "install", selector, "--scope", "user", "--yes"])
    plugins = _claude_json_array(
        command_for_platform([executable, "plugin", "list", "--json"]),
        "Claude plugin list",
    )
    matching_plugins = [
        item for item in plugins if item.get("id") == selector and item.get("enabled") is True
    ]
    if len(matching_plugins) != 1:
        raise CreationError(f"Claude did not install the expected plugin: {selector}")
    install_path = matching_plugins[0].get("installPath")
    if not isinstance(install_path, str) or not install_path:
        raise CreationError("installed Claude creation plugin has no install path")
    installed_skill = Path(install_path) / f"skills/{SKILL_NAME}/SKILL.md"
    expected_skill = Path(adapter["plugin_root"]) / f"skills/{SKILL_NAME}/SKILL.md"
    if not installed_skill.is_file() or installed_skill.read_bytes() != expected_skill.read_bytes():
        raise IntegrityError("installed Claude creation Skill does not match the PAL projection")


def install_creation_adapter(
    library_root: Path,
    cli_id: str,
    *,
    config_root: Path | None = None,
    repair: bool = False,
) -> dict[str, Any]:
    """Materialize and persist one creation entry through the target CLI."""

    from .installation_monitor import _creation_status, _inventory
    from .production_mount import _activation_lock

    config = resolve_config_root(config_root, create=True)
    with _activation_lock(config, "system-creation"):
        adapter = prepare_creation_adapter(library_root, cli_id, config_root=config)
        executable = require_compatible_cli(cli_id).executable
        if repair:
            state = _creation_status(
                cli_id,
                config,
                _inventory(cli_id, executable),
                _inventory(cli_id, executable, marketplaces=True),
            )
            if state["state"] == "healthy":
                return {**adapter, "installed": True, "executable": executable}
            if not state["repairable"]:
                raise CreationError(state["message"])
            if state["state"] != "missing":
                installed_marketplace = (
                    state.get("installed_marketplace") or adapter["marketplace_name"]
                )
                selector = f"{PLUGIN_NAME}@{installed_marketplace}"
                arguments = (
                    [executable, "plugin", "remove", selector, "--json"]
                    if cli_id == "codex"
                    else [executable, "plugin", "uninstall", selector]
                )
                _run_checked(arguments)
                if installed_marketplace != adapter["marketplace_name"]:
                    arguments = [
                        executable,
                        "plugin",
                        "marketplace",
                        "remove",
                        installed_marketplace,
                    ]
                    if cli_id == "codex":
                        arguments.append("--json")
                    _run_checked(arguments)
        if cli_id == "claude-code":
            _run_checked([executable, "plugin", "validate", "--strict", adapter["plugin_root"]])
            _ensure_claude_projection(executable, adapter)
        else:
            _ensure_codex_projection(executable, adapter)
        return {**adapter, "installed": True, "executable": executable}


def launch_creation_entry(
    library_root: Path,
    cli_id: str,
    *,
    config_root: Path | None = None,
    cli_arguments: Sequence[str] = (),
) -> int:
    """Prepare the adapter, enforce versions, and start a new CLI process."""

    root = canonical_existing_root(library_root)
    context = resolve_creation_context(root, config_root=config_root)
    pal_config_root = resolve_config_root(config_root, create=False)
    transition = pal_config_root / f"transitions/production/{context['library_id']}.json"
    if transition.exists() or is_link(transition):
        raise CreationError(
            f"cannot launch a creation entry during a production transition: {transition}"
        )
    adapter = install_creation_adapter(root, cli_id, config_root=config_root)
    executable = adapter["executable"]
    environment = os.environ.copy()
    environment["PAL_LIBRARY_ROOT"] = str(root)
    environment["PAL_INITIATING_CLI"] = cli_id
    if config_root is not None:
        environment["PAL_CONFIG_ROOT"] = str(resolve_config_root(config_root, create=False))

    if cli_id == "claude-code":
        _run_checked(
            [
                executable,
                "plugin",
                "validate",
                "--strict",
                adapter["plugin_root"],
            ]
        )
        command = [
            executable,
            "--plugin-dir",
            adapter["plugin_root"],
            *cli_arguments,
        ]
    else:
        command = [executable, *cli_arguments]
    return subprocess.call(command_for_platform(command), cwd=root, env=environment)


__all__ = [
    "CREATE_SKILL_TEMPLATE",
    "PLUGIN_NAME",
    "SKILL_NAME",
    "install_creation_adapter",
    "launch_creation_entry",
    "prepare_creation_adapter",
    "require_compatible_cli",
    "require_supported_cli_version",
]
