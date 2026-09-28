"""Configuration mount and creation-context acceptance tests.

Traceability: PRD-MOUNT-001, PRD-MOUNT-002, PRD-P0-004, PRD-TECH-001;
ACC-002, ACC-011, ACC-012; E2E-CONFIG-MOUNT-01.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import pal.config_mount as config_mount_module
from pal.config_mount import (
    bind_default_creation_library,
    mount_config,
    resolve_creation_context,
    resolve_default_creation_library,
)
from pal.creation import resolve_library_root
from pal.errors import PALError
from pal.io import sha256_file, tree_digest
from pal.library import initialize_library


def run_pal(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pal", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def parse_output(process: subprocess.CompletedProcess[str], stream: str) -> dict[str, object]:
    raw = process.stdout if stream == "stdout" else process.stderr
    return json.loads(raw)


def setup_library(tmp_path: Path) -> tuple[Path, Path]:
    library_root = tmp_path / "library"
    config_root = tmp_path / "pal-config"
    initialize_library(library_root, "config-acceptance-library")
    return library_root, config_root


def test_prd_tech_001_default_config_root_uses_linux_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_mount_module.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert config_mount_module.default_config_root() == tmp_path / "xdg/pal"


def test_prd_tech_001_default_config_root_rejects_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_mount_module.sys, "platform", "win32")

    with pytest.raises(PALError, match="macOS/POSIX"):
        config_mount_module.default_config_root()


def test_e2e_config_mount_01_acc_002_both_clis_resolve_one_development_context(
    tmp_path: Path,
) -> None:
    library_root, config_root = setup_library(tmp_path)
    production_before = tree_digest(library_root / "production")
    active_before = sha256_file(library_root / "production" / "active.json")

    for cli_id in ("claude-code", "codex"):
        process = run_pal(
            "mount",
            "config",
            "--library",
            str(library_root),
            "--cli",
            cli_id,
            "--config-root",
            str(config_root),
        )
        assert process.returncode == 0, process.stderr
        output = parse_output(process, "stdout")
        assert output["proof"] == "PAL_CONFIG_MOUNT_CREATED"
        assert output["library_id"] == "config-acceptance-library"
        assert output["cli_id"] == cli_id
        assert output["idempotent"] is False

    context = resolve_creation_context(library_root, config_root=config_root)
    assert context["target_clis"] == ["claude-code", "codex"]
    assert context["library_root"] == str(library_root.resolve())
    assert context["development_root"] == str((library_root / "development").resolve())
    assert context["production_root"] == str((library_root / "production").resolve())
    assert set(context["profile_ids"]) == {"claude-code", "codex"}
    assert context["profile_ids"]["claude-code"] == [
        "skill-md-v1-basic",
        "skill-md-v1-claude-model-pinned",
    ]
    assert context["profile_ids"]["codex"] == ["skill-md-v1-basic"]
    assert (
        context["mounts"]["claude-code"]["library_manifest_sha256"]
        == context["library_manifest_sha256"]
    )
    assert (
        context["mounts"]["codex"]["library_manifest_sha256"] == context["library_manifest_sha256"]
    )

    # Configuration mounting is not a production operation.
    assert tree_digest(library_root / "production") == production_before
    assert sha256_file(library_root / "production" / "active.json") == active_before


def test_prd_mount_001_acc_002_repeated_mount_is_idempotent(tmp_path: Path) -> None:
    library_root, config_root = setup_library(tmp_path)
    first = mount_config(library_root, "claude-code", config_root=config_root)
    record_path = Path(first["path"])
    before = record_path.read_bytes()

    second = mount_config(library_root, "claude-code", config_root=config_root)
    assert second["idempotent"] is True
    assert record_path.read_bytes() == before


def test_default_creation_binding_resolves_one_selected_library_without_guessing(
    tmp_path: Path,
) -> None:
    (tmp_path / "first").mkdir()
    first, config_root = setup_library(tmp_path / "first")
    for cli_id in ("claude-code", "codex"):
        mount_config(first, cli_id, config_root=config_root)
    second = tmp_path / "second/library"
    second.parent.mkdir()
    initialize_library(second, "second-library")
    for cli_id in ("claude-code", "codex"):
        mount_config(second, cli_id, config_root=config_root)

    binding = bind_default_creation_library(first, config_root=config_root)
    unrelated = tmp_path / "business-project"
    unrelated.mkdir()

    assert binding["idempotent"] is False
    assert resolve_default_creation_library(config_root=config_root) == first.resolve()
    assert resolve_library_root(None, cwd=unrelated, config_root=config_root) == first.resolve()
    assert bind_default_creation_library(first, config_root=config_root)["idempotent"] is True

    switched = bind_default_creation_library(second, config_root=config_root)
    assert switched["idempotent"] is False
    assert resolve_library_root(None, cwd=unrelated, config_root=config_root) == second.resolve()


def test_default_creation_binding_fails_closed_when_mount_digest_drifts(tmp_path: Path) -> None:
    library_root, config_root = setup_library(tmp_path)
    for cli_id in ("claude-code", "codex"):
        mount_config(library_root, cli_id, config_root=config_root)
    bind_default_creation_library(library_root, config_root=config_root)
    record_path = config_root / "mounts/config/config-acceptance-library/codex.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["verified_at"] = "2026-09-20T23:59:59Z"
    record_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(PALError):
        resolve_default_creation_library(config_root=config_root)


def test_prd_mount_001_acc_012_existing_different_record_is_not_overwritten(
    tmp_path: Path,
) -> None:
    library_root, config_root = setup_library(tmp_path)
    first = mount_config(library_root, "codex", config_root=config_root)
    record_path = Path(first["path"])
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["mount_id"] = "config-conflicting-record"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    before = record_path.read_bytes()

    with pytest.raises(PALError):
        mount_config(library_root, "codex", config_root=config_root)
    assert record_path.read_bytes() == before


def test_prd_mount_001_acc_012_missing_one_mount_blocks_context_resolution(
    tmp_path: Path,
) -> None:
    library_root, config_root = setup_library(tmp_path)
    mount_config(library_root, "claude-code", config_root=config_root)

    with pytest.raises(PALError):
        resolve_creation_context(library_root, config_root=config_root)


def test_prd_mount_001_acc_012_config_root_symlink_is_rejected(tmp_path: Path) -> None:
    library_root, config_root = setup_library(tmp_path)
    real_root = tmp_path / "real-config"
    real_root.mkdir()
    config_root.symlink_to(real_root, target_is_directory=True)

    process = run_pal(
        "mount",
        "config",
        "--library",
        str(library_root),
        "--cli",
        "claude-code",
        "--config-root",
        str(config_root),
    )
    assert process.returncode == 20
    assert parse_output(process, "stderr")["proof"] == "PAL_CONFIG_MOUNT_REJECTED"
    assert not list(real_root.rglob("*"))


def test_prd_mount_001_acc_012_library_reference_symlink_is_rejected(tmp_path: Path) -> None:
    library_root, config_root = setup_library(tmp_path)
    specification = library_root / "development/specifications/common/skill-v1.md"
    outside = tmp_path / "outside-spec.md"
    outside.write_text("outside\n", encoding="utf-8")
    specification.unlink()
    specification.symlink_to(outside)

    process = run_pal(
        "mount",
        "config",
        "--library",
        str(library_root),
        "--cli",
        "claude-code",
        "--config-root",
        str(config_root),
    )
    assert process.returncode == 20
    assert not config_root.exists()


def test_prd_mount_001_acc_012_manifest_digest_drift_is_rejected_without_mount(
    tmp_path: Path,
) -> None:
    library_root, config_root = setup_library(tmp_path)
    manifest_before = sha256_file(library_root / "library.json")
    specification = library_root / "development/specifications/common/skill-v1.md"
    specification.write_text(
        specification.read_text(encoding="utf-8") + "drift\n", encoding="utf-8"
    )

    process = run_pal(
        "mount",
        "config",
        "--library",
        str(library_root),
        "--cli",
        "codex",
        "--config-root",
        str(config_root),
    )
    assert process.returncode == 20
    assert parse_output(process, "stderr")["proof"] == "PAL_CONFIG_MOUNT_REJECTED"
    assert sha256_file(library_root / "library.json") == manifest_before
    assert not config_root.exists()


def test_prd_mount_001_acc_012_invalid_cli_is_normal_pal_rejection(tmp_path: Path) -> None:
    library_root, config_root = setup_library(tmp_path)
    process = run_pal(
        "mount",
        "config",
        "--library",
        str(library_root),
        "--cli",
        "unknown-cli",
        "--config-root",
        str(config_root),
    )
    assert process.returncode == 20
    assert parse_output(process, "stderr")["proof"] == "PAL_CONFIG_MOUNT_REJECTED"


def test_prd_mount_002_acc_012_tampered_mount_record_blocks_resolver(tmp_path: Path) -> None:
    library_root, config_root = setup_library(tmp_path)
    mount_config(library_root, "claude-code", config_root=config_root)
    mount_config(library_root, "codex", config_root=config_root)
    record_path = config_root / "mounts/config/config-acceptance-library/codex.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["target_clis"] = ["codex"]
    record_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(PALError):
        resolve_creation_context(library_root, config_root=config_root)


def test_program_upgrade_preserves_configuration_mount_and_default_binding(tmp_path, monkeypatch):
    """ACC-002/012: package upgrades must not invalidate durable configuration."""
    library, config = setup_library(tmp_path)
    monkeypatch.setattr(config_mount_module, "__version__", "0.1.0")
    paths = [
        Path(mount_config(library, cli, config_root=config)["path"])
        for cli in ("claude-code", "codex")
    ]
    binding = bind_default_creation_library(library, config_root=config)
    paths.append(Path(binding["path"]))
    before = {p: p.read_bytes() for p in paths}
    monkeypatch.setattr(config_mount_module, "__version__", "0.2.0")
    for cli in ("claude-code", "codex"):
        assert mount_config(library, cli, config_root=config)["idempotent"]
    assert bind_default_creation_library(library, config_root=config)["idempotent"]
    resolve_default_creation_library(config_root=config)
    assert {p: p.read_bytes() for p in paths} == before
    record = json.loads(paths[0].read_text())
    record["adapter_version"] = "99.0.0"
    paths[0].write_text(json.dumps(record))
    with pytest.raises(PALError, match="unsupported.*adapter_version"):
        resolve_creation_context(library, config_root=config)
