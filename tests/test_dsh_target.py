"""DeepSeek Harness target contract, projection and creation entry tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pal.adapters import install_creation_adapter, prepare_creation_adapter
from pal.compatibility import CliCompatibility, detect_cli_compatibility
from pal.config_mount import mount_config
from pal.creation import begin_creation, commit_creation
from pal.errors import PALError
from pal.io import formatted_json_bytes, tree_digest
from pal.library import initialize_library
from pal.production_mount import (
    _build_target_plan,
    _dsh_units,
    _install_dsh,
    _remove_dsh,
    _verify_dsh_installed,
)
from pal.publishing import compose_production, create_release


def _setup_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "dsh-home"
    home.mkdir()
    monkeypatch.setenv("DSH_HOME", str(home))
    library = tmp_path / "library"
    config = tmp_path / "config"
    initialize_library(library, "dsh-library")
    for cli_id in ("claude-code", "codex", "dsh"):
        mount_config(library, cli_id, config_root=config)
    return library, config


def _create_skill(tmp_path: Path, library: Path, config: Path, unit_id: str) -> str:
    request = {
        "schema_version": 1,
        "unit_id": unit_id,
        "summary": "DSH target test Skill",
        "profile_by_cli": {
            "claude-code": "skill-md-v1-shared",
            "codex": "skill-md-v1-shared",
            "dsh": "skill-md-v1-shared",
        },
    }
    request_path = tmp_path / f"{unit_id}.json"
    request_path.write_bytes(formatted_json_bytes(request))
    opened = begin_creation(library, "dsh", request_path, config_root=config)
    candidate = opened["candidates"][0]
    Path(candidate["skill_path"]).write_text(
        "---\n"
        f"name: {unit_id}\n"
        "description: DSH target test Skill.\n"
        "---\n\n"
        "# Instructions\n\n"
        "Return a deterministic result.\n",
        encoding="utf-8",
    )
    committed = commit_creation(library, opened["creation_id"], config_root=config)
    return committed["revision_id"]


def test_dsh_driver_has_no_executable_and_projects_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "dsh-home"
    home.mkdir()
    monkeypatch.setenv("DSH_HOME", str(home))
    compatibility = detect_cli_compatibility("dsh")
    assert compatibility.executable == ""
    assert compatibility.classification == "filesystem-root"
    assert compatibility.probes == ()


def test_dsh_creation_entry_is_installed_into_the_configured_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library, config = _setup_library(tmp_path, monkeypatch)
    adapter = prepare_creation_adapter(library, "dsh", config_root=config)
    result = install_creation_adapter(library, "dsh", config_root=config)
    assert result["installed"] is True
    home = Path(os.environ["DSH_HOME"])
    entry = home / "skills" / "pal-create-skill" / "SKILL.md"
    assert entry.is_file()
    assert "DSH" in entry.read_text(encoding="utf-8")
    assert tree_digest(home / "skills" / "pal-create-skill") == tree_digest(
        Path(adapter["projection_root"]) / "skills" / "pal-create-skill"
    )


def test_dsh_production_projection_installs_verifies_and_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library, config = _setup_library(tmp_path, monkeypatch)
    revision = _create_skill(tmp_path, library, config, "dsh-skill")
    release = create_release(library, "dsh-skill", revision)
    production = compose_production(library, [release["release_id"]])
    compatibilities = {
        cli_id: CliCompatibility(
            cli_id=cli_id,
            executable="",
            actual_version="0.0.0",
            minimum_version="0.0.0",
            classification="test",
            probes=(),
        )
        for cli_id in ("claude-code", "codex", "dsh")
    }
    plan = _build_target_plan(
        library,
        config,
        "dsh-library",
        production["production_version_id"],
        activation_time="2026-11-23T00:00:00Z",
        executables={},
        compatibilities=compatibilities,
    )
    assert plan.target_clis == ("claude-code", "codex", "dsh")
    assert set(_dsh_units(plan)) == {"dsh-skill"}
    _install_dsh(plan)
    assert _verify_dsh_installed(plan)["skill_count"] == 1
    home = Path(os.environ["DSH_HOME"])
    skill_file = home / "skills" / "dsh-skill" / "SKILL.md"
    assert skill_file.is_file()
    skill_file.write_bytes(b"drift\n")
    with pytest.raises(PALError):
        _verify_dsh_installed(plan)
    skill_file.write_bytes(plan.cli_projection_files["dsh"]["skills/dsh-skill/SKILL.md"])
    _remove_dsh(plan)
    assert not (home / "skills" / "dsh-skill").exists()
