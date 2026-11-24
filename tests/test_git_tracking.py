"""Private Git tracking setup tests.

Traceability: PRD-P0-002, PRD-P0-005; ACC-001, ACC-012.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import pal.production_mount as production_mount_module
from pal.errors import ProductionError
from pal.git_tracking import (
    GITATTRIBUTES_CONTENT,
    GITIGNORE_CONTENT,
    prepare_git_tracking,
)
from pal.production_mount import activate_production, adopt_active_production
from pal.transfer import export_library
from tests.test_production_mount import SimulatedCodex, compatible_targets
from tests.test_publishing import (
    commit_unit,
    compose_production,
    create_release,
    setup_library,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要本机 git 命令")


@pytest.fixture
def simulated_clis(monkeypatch: pytest.MonkeyPatch) -> SimulatedCodex:
    codex = SimulatedCodex()
    monkeypatch.setattr(
        production_mount_module,
        "_detect_target_compatibilities",
        compatible_targets,
    )
    monkeypatch.setattr(production_mount_module, "_run_claude_validation", lambda _plan: None)
    monkeypatch.setattr(production_mount_module, "_install_codex", codex.codex.install)
    monkeypatch.setattr(production_mount_module, "_remove_codex", codex.codex.remove)
    monkeypatch.setattr(production_mount_module, "_verify_codex_installed", codex.codex.verify)
    monkeypatch.setattr(production_mount_module, "_install_claude", codex.claude.install)
    monkeypatch.setattr(production_mount_module, "_remove_claude", codex.claude.remove)
    monkeypatch.setattr(production_mount_module, "_verify_claude_installed", codex.claude.verify)
    monkeypatch.setattr(
        production_mount_module,
        "_claude_installed_plugin_root",
        lambda plan, _installation: production_mount_module._plugin_root(plan, "claude-code"),
    )
    return codex


def published_library(tmp_path: Path, library_id: str):
    library_root, config_root = setup_library(tmp_path, library_id)
    revision_id = commit_unit(tmp_path, library_root, config_root, "git-skill")
    release = create_release(library_root, "git-skill", revision_id)
    production = compose_production(library_root, [release["release_id"]])
    activate_production(library_root, production["production_version_id"], config_root=config_root)
    return library_root, config_root, production["production_version_id"]


def test_git_init_writes_control_files_and_is_idempotent(
    tmp_path: Path, simulated_clis: SimulatedCodex
) -> None:
    library_root, _config_root, _version_id = published_library(tmp_path, "git-main")
    result = prepare_git_tracking(library_root)
    assert sorted(result["files_written"]) == [".gitattributes", ".gitignore"]
    assert result["repository_initialized"] is True
    assert (library_root / ".git").is_dir()
    assert (library_root / ".gitignore").read_text(encoding="utf-8") == GITIGNORE_CONTENT
    assert (library_root / ".gitattributes").read_text(encoding="utf-8") == GITATTRIBUTES_CONTENT
    assert any("git push -u origin HEAD" in step for step in result["next_steps"])

    again = prepare_git_tracking(library_root)
    assert again["files_written"] == []
    assert again["repository_initialized"] is False


def test_git_init_refuses_a_different_control_file(
    tmp_path: Path, simulated_clis: SimulatedCodex
) -> None:
    library_root, _config_root, _version_id = published_library(tmp_path, "git-conflict")
    (library_root / ".gitignore").write_text("custom\n", encoding="utf-8")
    with pytest.raises(ProductionError, match="内容不同"):
        prepare_git_tracking(library_root)


def test_git_init_keeps_an_identical_remote_and_rejects_a_different_one(
    tmp_path: Path, simulated_clis: SimulatedCodex
) -> None:
    library_root, _config_root, _version_id = published_library(tmp_path, "git-remote")
    remote = (tmp_path / "remote.git").as_uri()
    first = prepare_git_tracking(library_root, remote=remote)
    assert first["remote"] == remote
    again = prepare_git_tracking(library_root, remote=remote)
    assert again["remote"] == remote
    with pytest.raises(ProductionError, match="origin 已指向"):
        prepare_git_tracking(library_root, remote=(tmp_path / "other.git").as_uri())


def test_adopt_recreates_empty_skeleton_directories(
    tmp_path: Path, simulated_clis: SimulatedCodex
) -> None:
    library_root, config_root, version_id = published_library(tmp_path, "git-skeleton")
    (library_root / "records/usage").rmdir()
    (library_root / ".pal/transactions/usage").rmdir()
    shutil.rmtree(library_root / ".pal/locks")

    result = adopt_active_production(library_root, config_root=config_root)
    assert result["production_version_id"] == version_id
    assert (library_root / "records/usage").is_dir()
    assert (library_root / ".pal/transactions/usage").is_dir()
    assert (library_root / ".pal/locks").is_dir()


def test_export_skips_git_metadata(tmp_path: Path, simulated_clis: SimulatedCodex) -> None:
    library_root, _config_root, _version_id = published_library(tmp_path, "git-export")
    (library_root / ".git").mkdir()
    (library_root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (library_root / ".gitignore").write_text(".DS_Store\n", encoding="utf-8")
    archive = tmp_path / "git.tar.gz"
    export_library(library_root, archive)

    import tarfile

    with tarfile.open(archive, "r:gz") as handle:
        names = [member.name for member in handle]
    assert not any(name.startswith("library/.git/") for name in names)
    assert "library/.gitignore" in names
    assert "library/library.json" in names
