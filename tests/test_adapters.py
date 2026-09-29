"""Creation-entry projection and compatibility-gate tests.

Traceability: PRD-CLI-001 through PRD-CLI-003, PRD-MOUNT-001,
PRD-CREATE-001; ACC-003, ACC-004, ACC-012.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import pal.adapters as adapters_module
import pal.cli as cli_module
import pal.compatibility as compatibility_module
from pal.adapters import (
    install_creation_adapter,
    prepare_creation_adapter,
    require_compatible_cli,
    require_supported_cli_version,
)
from pal.config_mount import mount_config
from pal.errors import PALError
from pal.io import sha256_file, tree_digest
from pal.library import initialize_library
from pal.targets import target_driver


def setup_context(tmp_path: Path) -> tuple[Path, Path]:
    library_root = tmp_path / "library"
    config_root = tmp_path / "config"
    initialize_library(library_root, "adapter-library")
    mount_config(library_root, "claude-code", config_root=config_root)
    mount_config(library_root, "codex", config_root=config_root)
    return library_root, config_root


def run_pal(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pal", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def test_acc_003_004_both_adapter_shells_share_one_canonical_creation_skill(
    tmp_path: Path,
) -> None:
    library_root, config_root = setup_context(tmp_path)
    production_before = tree_digest(library_root / "production")
    active_before = sha256_file(library_root / "production/active.json")

    claude = prepare_creation_adapter(library_root, "claude-code", config_root=config_root)
    codex = prepare_creation_adapter(library_root, "codex", config_root=config_root)
    assert "/targets/creation/" in Path(claude["plugin_root"]).as_posix()
    assert claude["minimum_cli_version"] == "2.1.205"
    assert codex["minimum_cli_version"] == "0.147.0"
    claude_skill = Path(claude["plugin_root"]) / "skills/pal-create-skill/SKILL.md"
    codex_skill = Path(codex["plugin_root"]) / "skills/pal-create-skill/SKILL.md"
    claude_text = claude_skill.read_text(encoding="utf-8")
    codex_text = codex_skill.read_text(encoding="utf-8")
    assert "--cli claude-code" in claude_text
    assert "--cli codex" in codex_text
    assert str(config_root) in claude_text
    assert str(config_root) in codex_text
    assert "PAL_INITIATING_CLI" not in claude_text
    assert "PAL_LIBRARY_ROOT" not in codex_text
    assert "PAL_UNIT_PUBLISHED" in claude_text
    assert "For one Skill, select the requested action" in codex_text
    assert "`schema_version` must be the JSON number `1`" in claude_text
    assert "For an update, use the same four fields plus `base_revision_id`" in claude_text
    assert claude_text.replace("--cli claude-code", "--cli <target>") == codex_text.replace(
        "--cli codex", "--cli <target>"
    )
    assert "mktemp -d" in claude_text
    assert "Never" in claude_text
    assert "`skill_root`" in claude_text
    assert "`references/`, `scripts/`, or `assets/`" in claude_text
    assert "interpreter" in claude_text

    claude_manifest = json.loads(
        (Path(claude["plugin_root"]) / ".claude-plugin/plugin.json").read_text(encoding="utf-8")
    )
    codex_manifest = json.loads(
        (Path(codex["plugin_root"]) / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
    )
    assert claude_manifest["name"] == "pal"
    assert codex_manifest["name"] == "pal"
    assert ".codex-plugin" not in claude_manifest
    assert codex_manifest["skills"] == "./skills/"

    marketplace = json.loads(Path(codex["marketplace_manifest"]).read_text(encoding="utf-8"))
    entry = marketplace["plugins"][0]
    assert entry["source"] == {
        "source": "local",
        "path": "./plugins/pal",
    }
    assert entry["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }
    assert entry["category"] == "Productivity"
    claude_marketplace = json.loads(
        Path(claude["marketplace_manifest"]).read_text(encoding="utf-8")
    )
    assert claude_marketplace["name"] == claude["marketplace_name"]
    assert claude_marketplace["plugins"][0]["source"] == "./plugins/pal"

    assert tree_digest(library_root / "production") == production_before
    assert sha256_file(library_root / "production/active.json") == active_before


@pytest.mark.parametrize("cli_id", ["claude-code", "codex"])
def test_multifile_adapter_uses_new_projection_without_rewriting_old_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_id: str
) -> None:
    library, config = setup_context(tmp_path)
    with monkeypatch.context() as old:
        old.setattr(adapters_module, "CREATION_LAYOUT_VERSION", "native-v1")
        old.setattr(
            adapters_module, "CREATE_SKILL_TEMPLATE", "---\nname: pal-create-skill\n---\nLegacy\n"
        )
        legacy = prepare_creation_adapter(library, cli_id, config_root=config)
    old_root = config / "targets/creation/native-v1"
    old_digest = tree_digest(old_root)
    current = prepare_creation_adapter(library, cli_id, config_root=config)
    assert current["plugin_root"] != legacy["plugin_root"]
    assert "native-v8" in Path(current["plugin_root"]).parts
    assert tree_digest(old_root) == old_digest
    assert adapters_module._managed_creation_marketplace(Path(legacy["marketplace_root"]), cli_id)


def test_creation_adapter_requires_exact_update_target_and_change_request(
    tmp_path: Path,
) -> None:
    library_root, config_root = setup_context(tmp_path)
    adapter = prepare_creation_adapter(library_root, "codex", config_root=config_root)
    text = (Path(adapter["plugin_root"]) / "skills/pal-create-skill/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert adapters_module.CREATION_LAYOUT_VERSION == "native-v8"
    assert (
        f"{adapters_module.__version__}+{adapters_module.CREATION_ADAPTER_SUFFIX}"
        in json.loads(
            (Path(adapter["plugin_root"]) / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
        )["version"]
    )
    assert "exact existing Skill" in text
    assert "library ID" in text
    assert "pal create list --config-root" in text
    assert "pal skill preview --action" in text
    assert "pal skill apply --action" in text
    assert "不得执行" in text or "do not run `inspect`, `begin`," in text


def test_adapter_prepare_is_idempotent_and_rejects_drift(tmp_path: Path) -> None:
    library_root, config_root = setup_context(tmp_path)
    first = prepare_creation_adapter(library_root, "codex", config_root=config_root)
    second = prepare_creation_adapter(library_root, "codex", config_root=config_root)
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert first["tree_sha256"] == second["tree_sha256"]

    manifest = Path(first["plugin_root"]) / ".codex-plugin/plugin.json"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "drift\n", encoding="utf-8")
    with pytest.raises(PALError, match="drifted"):
        prepare_creation_adapter(library_root, "codex", config_root=config_root)


@pytest.mark.parametrize("cli_id", ["claude-code", "codex"])
def test_native_creation_install_uses_target_specific_persistent_installer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli_id: str,
) -> None:
    library_root, config_root = setup_context(tmp_path)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        adapters_module,
        "require_compatible_cli",
        lambda selected: SimpleNamespace(
            executable="claude" if selected == "claude-code" else "codex"
        ),
    )
    monkeypatch.setattr(
        adapters_module, "_run_checked", lambda arguments: calls.append(("validate", arguments[0]))
    )
    monkeypatch.setattr(
        adapters_module,
        "_ensure_claude_projection",
        lambda executable, _adapter: calls.append(("claude-code", executable)),
    )
    monkeypatch.setattr(
        adapters_module,
        "_ensure_codex_projection",
        lambda executable, _adapter: calls.append(("codex", executable)),
    )

    result = install_creation_adapter(library_root, cli_id, config_root=config_root)

    assert result["installed"] is True
    if cli_id == "claude-code":
        assert calls == [("validate", "claude"), ("claude-code", "claude")]
    else:
        assert calls == [("codex", "codex")]


@pytest.mark.parametrize("cli_id", ["claude-code", "codex"])
def test_launch_create_prepare_only_uses_formal_command_surface(
    tmp_path: Path,
    cli_id: str,
) -> None:
    library_root, config_root = setup_context(tmp_path)
    process = run_pal(
        "launch",
        "create",
        "--library",
        str(library_root),
        "--cli",
        cli_id,
        "--config-root",
        str(config_root),
        "--prepare-only",
    )
    assert process.returncode == 0, process.stderr
    output = json.loads(process.stdout)
    assert output["proof"] == "PAL_CREATION_ADAPTER_READY"
    assert output["cli_id"] == cli_id


def test_cli_compatibility_gate_accepts_verified_and_probed_newer_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        compatibility_module,
        "_isolated_codex_behavior_probe",
        lambda _executable: "isolated probe passed",
    )

    codex_version = "0.147.0"

    def supported_codex(arguments: list[str], **_kwargs: object) -> SimpleNamespace:
        suffix = arguments[1:]
        outputs = {
            ("--version",): f"codex-cli {codex_version}\n",
            ("--help",): "Commands: exec plugin\n",
            ("exec", "--help"): "--json --ephemeral\n",
            ("plugin", "list", "--help"): "--json\n",
            ("plugin", "add", "--help"): "--json PLUGIN[@MARKETPLACE]\n",
            ("plugin", "remove", "--help"): "--json PLUGIN[@MARKETPLACE]\n",
            ("plugin", "marketplace", "list", "--help"): "--json\n",
            ("plugin", "marketplace", "add", "--help"): "--json <SOURCE>\n",
            ("plugin", "marketplace", "remove", "--help"): "--json <MARKETPLACE_NAME>\n",
            ("plugin", "list", "--json"): '{"installed": []}',
            ("plugin", "marketplace", "list", "--json"): '{"marketplaces": []}',
        }
        return SimpleNamespace(returncode=0, stdout=outputs[tuple(suffix)], stderr="")

    monkeypatch.setattr(adapters_module.subprocess, "run", supported_codex)
    assert require_supported_cli_version("codex") == "codex"
    assert require_compatible_cli("codex").classification == "verified-version"

    codex_version = "0.154.0"
    assert require_compatible_cli("codex").classification == "verified-version"

    def newer_claude(arguments: list[str], **_kwargs: object) -> SimpleNamespace:
        suffix = arguments[1:]
        if suffix == ["--version"]:
            output = "2.1.237 (Claude Code)\n"
        elif suffix == ["--help"]:
            output = (
                "--plugin-dir --print --output-format stream-json --verbose "
                "--no-session-persistence --permission-mode\n"
            )
        elif suffix[-1] == "--json":
            output = "[]"
        else:
            output = "--strict --scope\n"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(adapters_module.subprocess, "run", newer_claude)
    assert require_supported_cli_version("claude-code") == "claude"
    assert require_compatible_cli("claude-code").classification == "probe-compatible-version"

    def below_minimum(arguments: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="2.1.204 (Claude Code)\n", stderr="")

    monkeypatch.setattr(adapters_module.subprocess, "run", below_minimum)
    with pytest.raises(PALError, match=r"minimum 2\.1\.205, got 2\.1\.204"):
        require_supported_cli_version("claude-code")


def test_missing_second_config_mount_blocks_adapter_materialization(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    config_root = tmp_path / "config"
    initialize_library(library_root, "incomplete-adapter-library")
    mount_config(library_root, "claude-code", config_root=config_root)

    with pytest.raises(PALError):
        prepare_creation_adapter(library_root, "claude-code", config_root=config_root)
    assert not (config_root / "targets").exists()


def test_cli_compatibility_probe_and_blacklist_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_surface(arguments: list[str], **_kwargs: object) -> SimpleNamespace:
        output = "2.1.237 (Claude Code)\n" if arguments[1:] == ["--version"] else "--plugin-dir\n"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(adapters_module.subprocess, "run", missing_surface)
    with pytest.raises(PALError, match="compatibility probe cli-help is missing"):
        require_compatible_cli("claude-code")

    driver = target_driver("claude-code")
    monkeypatch.setattr(driver, "incompatible_versions", frozenset({"2.1.237"}))
    with pytest.raises(PALError, match="known incompatible"):
        require_compatible_cli("claude-code")


def test_codex_isolated_probe_uses_portable_root_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def checked_json(
        arguments: list[str],
        *,
        environment: dict[str, str],
    ) -> dict[str, object]:
        suffix = arguments[1:]
        if suffix[:3] == ["plugin", "marketplace", "add"]:
            marketplace = Path(suffix[3])
            plugin = marketplace / "plugins/pal-compatibility-skill"
            manifest = json.loads((plugin / "plugin.json").read_text(encoding="utf-8"))
            observed.update(
                {
                    "schema": manifest["$schema"],
                    "legacy_exists": (plugin / ".codex-plugin/plugin.json").exists(),
                    "codex_home": environment["CODEX_HOME"],
                }
            )
            return {}
        if suffix == ["plugin", "list", "--json"]:
            return {
                "installed": [
                    {
                        "pluginId": ("pal-compatibility-skill@pal-compatibility-probe"),
                        "installed": True,
                    }
                ]
            }
        return {}

    monkeypatch.setattr(compatibility_module, "_checked_json_command", checked_json)

    detail = compatibility_module._isolated_codex_behavior_probe("codex")

    assert detail == "temporary CODEX_HOME portable plugin.json add/list/remove passed"
    assert observed["schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert observed["legacy_exists"] is False
    assert str(observed["codex_home"]).endswith("codex-home")


def test_compatibility_check_cli_emits_probe_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    compatibility = SimpleNamespace(
        diagnostic=lambda: {
            "cli_id": "claude-code",
            "actual_version": "2.1.237",
            "minimum_version": "2.1.205",
            "classification": "probe-compatible-version",
            "executable": "claude",
            "probes": [{"probe_id": "cli-help", "detail": "passed"}],
        }
    )
    monkeypatch.setattr(cli_module, "detect_cli_compatibility", lambda _cli_id: compatibility)

    assert cli_module.main(["compatibility", "check", "--cli", "claude-code"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["proof"] == "PAL_CLI_COMPATIBILITY_VERIFIED"
    assert output["classification"] == "probe-compatible-version"


def test_launch_create_strips_argument_separator_before_cli_passthrough(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library_root, config_root = setup_context(tmp_path)
    captured: dict[str, object] = {}

    def launch(
        library: Path,
        cli_id: str,
        *,
        config_root: Path | None = None,
        cli_arguments: tuple[str, ...] | list[str] = (),
    ) -> int:
        captured.update(
            {
                "library": library,
                "cli_id": cli_id,
                "config_root": config_root,
                "cli_arguments": list(cli_arguments),
            }
        )
        return 0

    monkeypatch.setattr(cli_module, "launch_creation_entry", launch)
    result = cli_module.main(
        [
            "launch",
            "create",
            "--library",
            str(library_root),
            "--cli",
            "claude-code",
            "--config-root",
            str(config_root),
            "--",
            "-p",
            "--tools",
            "Skill,Bash",
        ]
    )
    assert result == 0
    assert captured == {
        "library": library_root,
        "cli_id": "claude-code",
        "config_root": config_root,
        "cli_arguments": ["-p", "--tools", "Skill,Bash"],
    }
