"""P1 creation transaction, branching, and boundary tests.

Traceability: PRD-CLI-001 through PRD-CLI-003, PRD-SPEC-001/002,
PRD-CREATE-001 through PRD-CREATE-005; ACC-003 through ACC-006, ACC-012.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pal.config_mount import bind_default_creation_library, mount_config
from pal.creation import (
    abort_creation,
    begin_creation,
    commit_creation,
    resolve_library_root,
)
from pal.errors import PALError
from pal.io import formatted_json_bytes, sha256_file, tree_digest
from pal.library import doctor_library, initialize_library
from tests.test_publishing import commit_unit


@pytest.mark.parametrize("directed", [False, True])
def test_multifile_creation_commits_complete_inventory_without_changing_production(
    tmp_path: Path, directed: bool
) -> None:
    library, config = setup_context(tmp_path, "multifile-library")
    production_before = tree_digest(library / "production")
    extras = {
        "references/guide.txt": b"local reference\n",
        "scripts/read.py": b"print('local script')\n",
        "assets/template.bin": b"\x00\xff\x80",
    }
    revision = commit_unit(
        tmp_path, library, config, "files", directed=directed, extra_files=extras
    )
    artifacts = list(
        (library / f"development/units/files/revisions/{revision}").rglob("artifact.json")
    )
    assert len(artifacts) == (2 if directed else 1)
    for artifact_path in artifacts:
        artifact = json.loads(artifact_path.read_bytes())
        assert {entry["path"] for entry in artifact["files"]} == {
            "skills/files/SKILL.md",
            *(f"skills/files/{path}" for path in extras),
        }
        for relative, content in extras.items():
            assert (
                artifact_path.parent / f"payload/skills/files/{relative}"
            ).read_bytes() == content
    assert tree_digest(library / "production") == production_before
    assert not any((library / "releases/units").iterdir())
    assert doctor_library(library)["active_production_version_id"] is None


@pytest.mark.parametrize(
    "invalid",
    [
        "orphan",
        "payload-file",
        "other-skill",
        "empty-other-skill",
        "file-link",
        "directory-link",
        "fifo",
        "bad-name",
        "missing-canonical",
        "directory-canonical",
    ],
)
def test_multifile_candidate_rejects_unsafe_or_unowned_entries(
    tmp_path: Path, invalid: str
) -> None:
    library, config = setup_context(tmp_path, "invalid-files")
    request = tmp_path / "request.json"
    write_request(request, "files")
    opened = begin_creation(library, "codex", request, config_root=config)
    candidate = opened["candidates"][0]
    canonical = Path(candidate["skill_path"])
    skill_root = Path(candidate["skill_root"])
    assert canonical.parent == skill_root
    canonical.write_text(basic_skill("files"))
    transaction_root = Path(opened["transaction_path"]).parent
    if invalid == "orphan":
        (transaction_root / "generated/orphan.txt").write_bytes(b"outside artifacts")
    elif invalid == "payload-file":
        (skill_root.parent.parent / "outside.txt").write_bytes(b"outside Skill")
    elif invalid in {"other-skill", "empty-other-skill"}:
        other = skill_root.parent / "other"
        other.mkdir()
        if invalid == "other-skill":
            (other / "SKILL.md").write_text(basic_skill("other"))
    elif invalid in {"file-link", "directory-link"}:
        outside = tmp_path / "outside"
        if invalid == "file-link":
            outside.write_bytes(b"outside")
        else:
            outside.mkdir()
        (skill_root / "link").symlink_to(outside)
    elif invalid == "fifo":
        os.mkfifo(skill_root / "pipe")
    elif invalid == "bad-name":
        (skill_root / "bad\\name").write_bytes(b"unsafe")
    else:
        canonical.unlink()
        if invalid == "directory-canonical":
            canonical.mkdir()
    production_before = tree_digest(library / "production")
    with pytest.raises(PALError):
        commit_creation(library, opened["creation_id"], config_root=config)
    assert not (library / "development/units/files").exists()
    assert tree_digest(library / "production") == production_before
    assert json.loads(Path(opened["transaction_path"]).read_bytes())["state"] == "ABORTED"


def run_pal(*arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pal", *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def setup_context(tmp_path: Path, library_id: str) -> tuple[Path, Path]:
    library_root = tmp_path / "library"
    config_root = tmp_path / "config"
    initialize_library(library_root, library_id)
    mount_config(library_root, "claude-code", config_root=config_root)
    mount_config(library_root, "codex", config_root=config_root)
    return library_root, config_root


def write_request(
    path: Path,
    unit_id: str,
    *,
    claude_profile: str = "skill-md-v1-basic",
    codex_profile: str = "skill-md-v1-basic",
) -> None:
    request = {
        "schema_version": 1,
        "unit_id": unit_id,
        "summary": f"Create {unit_id} for both supported CLIs",
        "profile_by_cli": {
            "claude-code": claude_profile,
            "codex": codex_profile,
        },
    }
    path.write_bytes(formatted_json_bytes(request))


def basic_skill(unit_id: str) -> str:
    return (
        "---\n"
        f"name: {unit_id}\n"
        "description: Perform the requested deterministic test workflow.\n"
        "---\n\n"
        "# Instructions\n\nReturn the requested result.\n"
    )


def model_skill(unit_id: str) -> str:
    return (
        "---\n"
        f"name: {unit_id}\n"
        "description: Perform the Claude model-pinned test workflow.\n"
        "model: haiku\n"
        "---\n\n"
        "# Instructions\n\nReturn the requested result.\n"
    )


def test_native_cli_creation_resolves_quickstart_default_without_pal_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library_root, config_root = setup_context(tmp_path, "native-default-library")
    bind_default_creation_library(library_root, config_root=config_root)
    business_root = tmp_path / "business-project"
    business_root.mkdir()
    request_path = tmp_path / "request.json"
    write_request(request_path, "native-default-skill")
    for name in ("PAL_LIBRARY_ROOT", "PAL_INITIATING_CLI", "PAL_CONFIG_ROOT"):
        monkeypatch.delenv(name, raising=False)

    assert (
        resolve_library_root(None, cwd=business_root, config_root=config_root)
        == library_root.resolve()
    )
    process = run_pal(
        "create",
        "begin",
        "--cli",
        "codex",
        "--request-file",
        str(request_path),
        "--config-root",
        str(config_root),
        cwd=business_root,
    )

    assert process.returncode == 0, process.stderr
    output = json.loads(process.stdout)
    assert output["proof"] == "PAL_CREATION_OPENED"
    assert output["library_id"] == "native-default-library"
    transaction = json.loads(Path(output["transaction_path"]).read_text(encoding="utf-8"))
    assert transaction["created_by_cli"] == "codex"
    assert doctor_library(library_root)["active_production_version_id"] is None


@pytest.mark.parametrize("created_by_cli", ["claude-code", "codex"])
def test_acc_003_004_005_shared_creation_commits_one_cross_cli_artifact(
    tmp_path: Path,
    created_by_cli: str,
) -> None:
    library_root, config_root = setup_context(tmp_path, f"shared-{created_by_cli}")
    request_path = tmp_path / "request.json"
    unit_id = f"shared-from-{created_by_cli}"
    write_request(request_path, unit_id)
    production_before = tree_digest(library_root / "production")
    active_before = sha256_file(library_root / "production/active.json")

    opened = begin_creation(
        library_root,
        created_by_cli,
        request_path,
        config_root=config_root,
    )
    assert len(opened["candidates"]) == 1
    assert set(opened["context"]["references"]["cli_specifications"]) == {
        "claude-code",
        "codex",
    }
    assert opened["context"]["references"]["common_specifications"]
    assert opened["context"]["references"]["design_rules"]
    candidate = opened["candidates"][0]
    assert candidate["profile_id"] == "skill-md-v1-basic"
    assert candidate["covered_clis"] == ["claude-code", "codex"]
    Path(candidate["skill_path"]).write_text(basic_skill(unit_id), encoding="utf-8")

    committed = commit_creation(
        library_root,
        opened["creation_id"],
        config_root=config_root,
    )
    assert committed["unit_id"] == unit_id
    assert committed["idempotent"] is False
    assert len(committed["artifacts"]) == 1
    assert committed["artifacts"][0]["covered_clis"] == ["claude-code", "codex"]

    unit_root = library_root / "development/units" / unit_id
    unit = json.loads((unit_root / "unit.json").read_text(encoding="utf-8"))
    revision_root = unit_root / "revisions" / unit["current_revision_id"]
    revision = json.loads((revision_root / "revision.json").read_text(encoding="utf-8"))
    assert revision["created_by_cli"] == created_by_cli
    assert revision["target_clis"] == ["claude-code", "codex"]
    assert len(revision["artifacts"]) == 1
    assert len(revision["specification_refs"]) == 4
    assert len(revision["profile_refs"]) == 1

    transaction = json.loads(
        (
            library_root / ".pal/transactions/creation" / opened["creation_id"] / "transaction.json"
        ).read_text(encoding="utf-8")
    )
    assert transaction["state"] == "COMMITTED"
    repeated = commit_creation(
        library_root,
        opened["creation_id"],
        config_root=config_root,
    )
    assert repeated["idempotent"] is True

    assert tree_digest(library_root / "production") == production_before
    assert sha256_file(library_root / "production/active.json") == active_before
    assert not any((library_root / "releases/units").iterdir())
    assert doctor_library(library_root)["active_production_version_id"] is None


def test_acc_006_directed_creation_commits_two_disjoint_profile_artifacts(
    tmp_path: Path,
) -> None:
    library_root, config_root = setup_context(tmp_path, "directed-library")
    request_path = tmp_path / "request.json"
    write_request(
        request_path,
        "directed-skill",
        claude_profile="skill-md-v1-claude-model-pinned",
    )
    opened = begin_creation(
        library_root,
        "codex",
        request_path,
        config_root=config_root,
    )
    assert len(opened["candidates"]) == 2
    by_profile = {candidate["profile_id"]: candidate for candidate in opened["candidates"]}
    Path(by_profile["skill-md-v1-claude-model-pinned"]["skill_path"]).write_text(
        model_skill("directed-skill"), encoding="utf-8"
    )
    Path(by_profile["skill-md-v1-basic"]["skill_path"]).write_text(
        basic_skill("directed-skill"), encoding="utf-8"
    )

    committed = commit_creation(
        library_root,
        opened["creation_id"],
        config_root=config_root,
    )
    assert len(committed["artifacts"]) == 2
    coverage = [cli for artifact in committed["artifacts"] for cli in artifact["covered_clis"]]
    assert coverage == ["claude-code", "codex"]
    assert {artifact["profile_id"] for artifact in committed["artifacts"]} == {
        "skill-md-v1-basic",
        "skill-md-v1-claude-model-pinned",
    }


def test_acc_012_incomplete_candidate_aborts_without_formal_unit_or_production_change(
    tmp_path: Path,
) -> None:
    library_root, config_root = setup_context(tmp_path, "incomplete-library")
    request_path = tmp_path / "request.json"
    write_request(
        request_path,
        "incomplete-skill",
        claude_profile="skill-md-v1-claude-model-pinned",
    )
    production_before = tree_digest(library_root / "production")
    opened = begin_creation(
        library_root,
        "claude-code",
        request_path,
        config_root=config_root,
    )
    Path(opened["candidates"][0]["skill_path"]).write_text(
        model_skill("incomplete-skill"), encoding="utf-8"
    )

    with pytest.raises(PALError):
        commit_creation(library_root, opened["creation_id"], config_root=config_root)

    transaction_path = (
        library_root / ".pal/transactions/creation" / opened["creation_id"] / "transaction.json"
    )
    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
    assert transaction["state"] == "ABORTED"
    assert not (library_root / "development/units/incomplete-skill").exists()
    assert tree_digest(library_root / "production") == production_before


def test_acc_012_wrong_profile_frontmatter_is_rejected(tmp_path: Path) -> None:
    library_root, config_root = setup_context(tmp_path, "wrong-profile-library")
    request_path = tmp_path / "request.json"
    write_request(request_path, "wrong-profile-skill")
    opened = begin_creation(
        library_root,
        "codex",
        request_path,
        config_root=config_root,
    )
    Path(opened["candidates"][0]["skill_path"]).write_text(
        model_skill("wrong-profile-skill"), encoding="utf-8"
    )

    with pytest.raises(PALError, match="fields"):
        commit_creation(library_root, opened["creation_id"], config_root=config_root)
    assert not (library_root / "development/units/wrong-profile-skill").exists()


def test_acc_012_mount_drift_aborts_before_development_commit(tmp_path: Path) -> None:
    library_root, config_root = setup_context(tmp_path, "mount-drift-library")
    request_path = tmp_path / "request.json"
    write_request(request_path, "mount-drift-skill")
    opened = begin_creation(
        library_root,
        "claude-code",
        request_path,
        config_root=config_root,
    )
    Path(opened["candidates"][0]["skill_path"]).write_text(
        basic_skill("mount-drift-skill"), encoding="utf-8"
    )
    codex_mount = config_root / "mounts/config/mount-drift-library/codex.json"
    mount_record = json.loads(codex_mount.read_text(encoding="utf-8"))
    mount_record["mount_id"] = "tampered-codex-mount"
    codex_mount.write_text(json.dumps(mount_record), encoding="utf-8")

    with pytest.raises(PALError):
        commit_creation(library_root, opened["creation_id"], config_root=config_root)
    assert not (library_root / "development/units/mount-drift-skill").exists()


def test_prd_create_005_p1_commit_does_not_read_or_change_active_pointer(
    tmp_path: Path,
) -> None:
    library_root, config_root = setup_context(tmp_path, "active-independent-library")
    request_path = tmp_path / "request.json"
    write_request(request_path, "active-independent-skill")
    opened = begin_creation(
        library_root,
        "codex",
        request_path,
        config_root=config_root,
    )
    Path(opened["candidates"][0]["skill_path"]).write_text(
        basic_skill("active-independent-skill"), encoding="utf-8"
    )
    active_path = library_root / "production/active.json"
    active_path.write_text("production transition in progress\n", encoding="utf-8")
    active_before = active_path.read_bytes()

    committed = commit_creation(
        library_root,
        opened["creation_id"],
        config_root=config_root,
    )
    assert committed["unit_id"] == "active-independent-skill"
    assert active_path.read_bytes() == active_before


def test_create_abort_is_idempotent_and_cannot_abort_committed(tmp_path: Path) -> None:
    library_root, config_root = setup_context(tmp_path, "abort-library")
    request_path = tmp_path / "request.json"
    write_request(request_path, "aborted-skill")
    opened = begin_creation(
        library_root,
        "codex",
        request_path,
        config_root=config_root,
    )
    first = abort_creation(library_root, opened["creation_id"])
    second = abort_creation(library_root, opened["creation_id"])
    assert first["idempotent"] is False
    assert second["idempotent"] is True


def test_create_cli_resolves_library_upward_and_commits(tmp_path: Path) -> None:
    library_root, config_root = setup_context(tmp_path, "cli-library")
    request_path = tmp_path / "request.json"
    write_request(request_path, "cli-created-skill")
    working_directory = library_root / "development/specifications"
    opened_process = run_pal(
        "create",
        "begin",
        "--cli",
        "codex",
        "--request-file",
        str(request_path),
        "--config-root",
        str(config_root),
        cwd=working_directory,
    )
    assert opened_process.returncode == 0, opened_process.stderr
    opened = json.loads(opened_process.stdout)
    assert opened["proof"] == "PAL_CREATION_OPENED"
    Path(opened["candidates"][0]["skill_path"]).write_text(
        basic_skill("cli-created-skill"), encoding="utf-8"
    )

    committed_process = run_pal(
        "create",
        "commit",
        "--creation",
        opened["creation_id"],
        "--config-root",
        str(config_root),
        cwd=working_directory,
    )
    assert committed_process.returncode == 0, committed_process.stderr
    assert json.loads(committed_process.stdout)["proof"] == "PAL_CREATION_COMMITTED"
