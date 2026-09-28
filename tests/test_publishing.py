"""Release and production composition lifecycle tests.

Traceability: PRD-RELEASE-001, PRD-RELEASE-002; ACC-007, ACC-011, ACC-012.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import pal.publishing as publishing_module
from pal.config_mount import mount_config
from pal.creation import begin_creation, commit_creation
from pal.errors import PALError
from pal.io import formatted_json_bytes, sha256_file, tree_digest
from pal.library import initialize_library
from pal.publishing import (
    compose_production,
    create_release,
    validate_production_version,
    validate_release,
)


def run_pal(*arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pal", *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def setup_library(tmp_path: Path, library_id: str) -> tuple[Path, Path]:
    library_root = tmp_path / "library"
    config_root = tmp_path / "config"
    initialize_library(library_root, library_id)
    mount_config(library_root, "claude-code", config_root=config_root)
    mount_config(library_root, "codex", config_root=config_root)
    return library_root, config_root


def skill_text(unit_id: str, *, model: str | None = None) -> str:
    model_line = f"model: {model}\n" if model is not None else ""
    return (
        "---\n"
        f"name: {unit_id}\n"
        "description: Exercise the immutable publishing workflow.\n"
        f"{model_line}"
        "---\n\n"
        "# Instructions\n\n"
        f"Return the deterministic result for {unit_id}.\n"
    )


def commit_unit(
    tmp_path: Path,
    library_root: Path,
    config_root: Path,
    unit_id: str,
    *,
    directed: bool = False,
    extra_files: dict[str, bytes] | None = None,
) -> str:
    request = {
        "schema_version": 1,
        "unit_id": unit_id,
        "summary": f"Create {unit_id} for publishing tests",
        "profile_by_cli": {
            "claude-code": ("skill-md-v1-claude-model-pinned" if directed else "skill-md-v1-basic"),
            "codex": "skill-md-v1-basic",
        },
    }
    request_path = tmp_path / f"{unit_id}.request.json"
    request_path.write_bytes(formatted_json_bytes(request))
    opened = begin_creation(
        library_root,
        "codex",
        request_path,
        config_root=config_root,
    )
    for candidate in opened["candidates"]:
        material = (
            skill_text(unit_id, model="haiku")
            if candidate["profile_id"] == "skill-md-v1-claude-model-pinned"
            else skill_text(unit_id)
        )
        Path(candidate["skill_path"]).write_text(material, encoding="utf-8")
        for relative, content in (extra_files or {}).items():
            path = Path(candidate["skill_root"]) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    committed = commit_creation(
        library_root,
        opened["creation_id"],
        config_root=config_root,
    )
    return committed["revision_id"]


def development_skill(library_root: Path, unit_id: str, revision_id: str) -> Path:
    matches = list(
        (
            library_root / "development/units" / unit_id / "revisions" / revision_id / "artifacts"
        ).glob(f"*/payload/skills/{unit_id}/SKILL.md")
    )
    assert len(matches) >= 1
    return matches[0]


def test_acc_007_release_and_production_compose_are_immutable_and_idempotent(
    tmp_path: Path,
) -> None:
    library_root, config_root = setup_library(tmp_path, "publishing-main")
    revision_id = commit_unit(
        tmp_path,
        library_root,
        config_root,
        "published-skill",
    )
    active_path = library_root / "production/active.json"
    active_before = active_path.read_bytes()

    released = create_release(library_root, "published-skill", revision_id)
    assert released["idempotent"] is False
    assert released["release_id"].startswith("release-")
    assert released["artifacts"][0]["covered_clis"] == ["claude-code", "codex"]
    assert (
        validate_release(
            library_root,
            "published-skill",
            released["release_id"],
        )["manifest_sha256"]
        == released["manifest_sha256"]
    )
    assert active_path.read_bytes() == active_before

    composed = compose_production(library_root, [released["release_id"]])
    assert composed["idempotent"] is False
    assert composed["production_version_id"].startswith("production-")
    assert composed["release_ids"] == [released["release_id"]]
    assert len(composed["artifact_ids"]) == 1
    validated = validate_production_version(
        library_root,
        composed["production_version_id"],
    )
    assert validated["manifest_sha256"] == composed["manifest_sha256"]
    assert "development/" not in Path(composed["manifest"]).read_text(encoding="utf-8")
    assert active_path.read_bytes() == active_before

    assert create_release(library_root, "published-skill", revision_id)["idempotent"] is True
    assert compose_production(library_root, [released["release_id"]])["idempotent"] is True

    release_tree_before = tree_digest(Path(released["manifest"]).parent)
    production_tree_before = tree_digest(Path(composed["manifest"]).parent)
    development_skill(library_root, "published-skill", revision_id).write_text(
        skill_text("published-skill") + "\nDevelopment-only drift.\n",
        encoding="utf-8",
    )
    assert tree_digest(Path(released["manifest"]).parent) == release_tree_before
    assert tree_digest(Path(composed["manifest"]).parent) == production_tree_before
    validate_production_version(library_root, composed["production_version_id"])
    assert active_path.read_bytes() == active_before


def test_acc_007_directed_release_preserves_two_disjoint_artifacts(tmp_path: Path) -> None:
    library_root, config_root = setup_library(tmp_path, "publishing-directed")
    revision_id = commit_unit(
        tmp_path,
        library_root,
        config_root,
        "directed-published",
        directed=True,
    )
    released = create_release(library_root, "directed-published", revision_id)
    assert len(released["artifacts"]) == 2
    assert [
        cli_id for artifact in released["artifacts"] for cli_id in artifact["covered_clis"]
    ] == ["claude-code", "codex"]

    composed = compose_production(library_root, [released["release_id"]])
    validated = validate_production_version(
        library_root,
        composed["production_version_id"],
    )
    assert len(validated["production"]["artifacts"]) == 2


def test_acc_007_old_releases_and_production_versions_are_retained(tmp_path: Path) -> None:
    library_root, config_root = setup_library(tmp_path, "publishing-history")
    first_revision = commit_unit(tmp_path, library_root, config_root, "first-skill")
    first_release = create_release(library_root, "first-skill", first_revision)
    first_production = compose_production(library_root, [first_release["release_id"]])
    first_release_digest = tree_digest(Path(first_release["manifest"]).parent)
    first_production_digest = tree_digest(Path(first_production["manifest"]).parent)

    second_revision = commit_unit(tmp_path, library_root, config_root, "second-skill")
    second_release = create_release(library_root, "second-skill", second_revision)
    second_production = compose_production(
        library_root,
        [second_release["release_id"], first_release["release_id"]],
    )

    assert first_production["production_version_id"] != second_production["production_version_id"]
    assert tree_digest(Path(first_release["manifest"]).parent) == first_release_digest
    assert tree_digest(Path(first_production["manifest"]).parent) == first_production_digest
    assert set(second_production["release_ids"]) == {
        first_release["release_id"],
        second_release["release_id"],
    }
    assert len(list((library_root / "production/versions").iterdir())) == 2


def test_acc_012_development_artifact_drift_blocks_release_without_side_effect(
    tmp_path: Path,
) -> None:
    library_root, config_root = setup_library(tmp_path, "release-source-drift")
    revision_id = commit_unit(tmp_path, library_root, config_root, "drifted-source")
    development_skill(library_root, "drifted-source", revision_id).write_text(
        skill_text("drifted-source") + "\nTampered.\n",
        encoding="utf-8",
    )
    active_before = sha256_file(library_root / "production/active.json")

    with pytest.raises(PALError, match="inventory|tree|SHA-256"):
        create_release(library_root, "drifted-source", revision_id)

    assert not (library_root / "releases/units/drifted-source").exists()
    assert sha256_file(library_root / "production/active.json") == active_before


def test_acc_012_release_drift_blocks_composition_and_preserves_active(tmp_path: Path) -> None:
    library_root, config_root = setup_library(tmp_path, "release-drift")
    revision_id = commit_unit(tmp_path, library_root, config_root, "release-drifted")
    released = create_release(library_root, "release-drifted", revision_id)
    release_skill = next(Path(released["manifest"]).parent.glob("payload/**/SKILL.md"))
    release_skill.write_text(skill_text("release-drifted") + "\nTampered.\n", encoding="utf-8")
    active_before = sha256_file(library_root / "production/active.json")

    with pytest.raises(PALError, match="SHA-256|inventory|tree"):
        compose_production(library_root, [released["release_id"]])

    assert not any((library_root / "production/versions").iterdir())
    assert sha256_file(library_root / "production/active.json") == active_before


def test_acc_012_production_drift_is_rejected_instead_of_overwritten(tmp_path: Path) -> None:
    library_root, config_root = setup_library(tmp_path, "production-drift")
    revision_id = commit_unit(tmp_path, library_root, config_root, "production-drifted")
    released = create_release(library_root, "production-drifted", revision_id)
    composed = compose_production(library_root, [released["release_id"]])
    production_skill = next(Path(composed["manifest"]).parent.glob("payload/**/SKILL.md"))
    production_skill.write_text(
        skill_text("production-drifted") + "\nTampered.\n",
        encoding="utf-8",
    )

    with pytest.raises(PALError, match="SHA-256|inventory|tree"):
        compose_production(library_root, [released["release_id"]])
    assert production_skill.read_text(encoding="utf-8").endswith("Tampered.\n")


def test_acc_012_compose_failure_cleans_staging_and_never_changes_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library_root, config_root = setup_library(tmp_path, "compose-atomic")
    revision_id = commit_unit(tmp_path, library_root, config_root, "atomic-skill")
    released = create_release(library_root, "atomic-skill", revision_id)
    active_before = (library_root / "production/active.json").read_bytes()

    def fail_rename(_source: Path, _target: Path) -> None:
        raise OSError("injected production commit failure")

    monkeypatch.setattr(publishing_module.os, "rename", fail_rename)
    with pytest.raises(OSError, match="injected production commit failure"):
        compose_production(library_root, [released["release_id"]])

    versions_root = library_root / "production/versions"
    assert not any(versions_root.iterdir())
    assert (library_root / "production/active.json").read_bytes() == active_before


def test_release_and_compose_do_not_read_or_repair_active_pointer(tmp_path: Path) -> None:
    library_root, config_root = setup_library(tmp_path, "publishing-active-independent")
    revision_id = commit_unit(tmp_path, library_root, config_root, "active-independent-release")
    active_path = library_root / "production/active.json"
    active_path.write_text("production activation is intentionally unavailable\n", encoding="utf-8")
    active_before = active_path.read_bytes()

    released = create_release(library_root, "active-independent-release", revision_id)
    composed = compose_production(library_root, [released["release_id"]])

    assert composed["release_ids"] == [released["release_id"]]
    assert active_path.read_bytes() == active_before


def test_release_and_compose_cli_support_upward_library_discovery(tmp_path: Path) -> None:
    library_root, config_root = setup_library(tmp_path, "publishing-cli")
    revision_id = commit_unit(tmp_path, library_root, config_root, "cli-published")
    working_directory = library_root / "development/specifications"

    release_process = run_pal(
        "release",
        "create",
        "--unit",
        "cli-published",
        "--revision",
        revision_id,
        cwd=working_directory,
    )
    assert release_process.returncode == 0, release_process.stderr
    released = json.loads(release_process.stdout)
    assert released["proof"] == "PAL_RELEASE_CREATED"

    compose_process = run_pal(
        "production",
        "compose",
        "--release",
        released["release_id"],
        cwd=working_directory,
    )
    assert compose_process.returncode == 0, compose_process.stderr
    composed = json.loads(compose_process.stdout)
    assert composed["proof"] == "PAL_PRODUCTION_COMPOSED"
    assert composed["release_ids"] == [released["release_id"]]


def test_compose_rejects_duplicate_or_missing_release_ids(tmp_path: Path) -> None:
    library_root, config_root = setup_library(tmp_path, "compose-inputs")
    revision_id = commit_unit(tmp_path, library_root, config_root, "compose-input-skill")
    released = create_release(library_root, "compose-input-skill", revision_id)

    with pytest.raises(PALError, match="unique"):
        compose_production(
            library_root,
            [released["release_id"], released["release_id"]],
        )
    with pytest.raises(PALError, match="does not exist"):
        compose_production(library_root, ["release-missing"])
    assert not any((library_root / "production/versions").iterdir())
