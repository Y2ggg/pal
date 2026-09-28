"""PSM-003; PRD-CREATE-004; ACC-006/012 update and recovery contracts."""

import json
import os
from pathlib import Path

import pytest

import pal.library as library_module
from pal import creation
from pal.cli import main
from pal.config_mount import bind_default_creation_library
from pal.creation import (
    abort_creation,
    begin_creation,
    commit_creation,
    inspect_creation,
    list_creation_units,
)
from pal.errors import PALError
from pal.io import tree_digest
from tests.test_creation import basic_skill, run_pal, setup_context, write_request


def initial(tmp_path):
    library, config = setup_context(tmp_path, "update-library")
    request = tmp_path / "request.json"
    write_request(request, "example")
    opened = begin_creation(library, "claude-code", request, config_root=config)
    skill = Path(opened["candidates"][0]["skill_path"])
    skill.write_text(basic_skill("example"))
    (skill.parent / "asset.bin").write_bytes(b"\x00\xff")
    commit_creation(library, opened["creation_id"], config_root=config)
    return library, config, opened


def update(tmp_path, library, config):
    base = inspect_creation(library, "example", config_root=config)
    request = tmp_path / "update.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "unit_id": "example",
                "summary": "Update example",
                "profile_by_cli": base["profile_by_cli"],
                "base_revision_id": base["revision_id"],
            }
        )
    )
    return begin_creation(library, "codex", request, config_root=config)


def test_update_preserves_bundle_history_and_production(tmp_path):
    library, config, first = initial(tmp_path)
    production = tree_digest(library / "production")
    revision = library / f"development/units/example/revisions/{first['revision_id']}"
    before = tree_digest(revision)
    opened = update(tmp_path, library, config)
    skill = Path(opened["candidates"][0]["skill_path"])
    assert (skill.parent / "asset.bin").read_bytes() == b"\x00\xff"
    skill.write_text(skill.read_text() + "\nUpdated behavior.\n")
    result = commit_creation(library, opened["creation_id"], config_root=config)
    unit = json.loads((revision.parent.parent / "unit.json").read_text())
    assert unit["revision_ids"] == [first["revision_id"], result["revision_id"]]
    assert tree_digest(revision) == before
    assert tree_digest(library / "production") == production
    assert commit_creation(library, first["creation_id"], config_root=config)["idempotent"]
    assert commit_creation(library, opened["creation_id"], config_root=config)["idempotent"]


@pytest.mark.parametrize("unit_id", ["update-library", "unknown-skill"])
def test_inspect_rejects_unknown_target_with_candidates_without_writing(tmp_path, unit_id):
    library, config, _ = initial(tmp_path)
    before = tree_digest(library)
    with pytest.raises(PALError, match="example.*pal create list") as error:
        inspect_creation(library, unit_id, config_root=config)
    assert ("外挂库 ID" in str(error.value)) == (unit_id == "update-library")
    assert str(config) in str(error.value)
    assert tree_digest(library) == before


def test_list_and_inspect_cli_resolve_default_library_from_empty_directory(tmp_path, monkeypatch):
    library, config, first = initial(tmp_path)
    bind_default_creation_library(library, config_root=config)
    workspace = tmp_path / "empty-workspace"
    workspace.mkdir()
    monkeypatch.delenv("PAL_LIBRARY_ROOT", raising=False)
    before = tree_digest(library)
    config_before = tree_digest(config)
    listed = run_pal("create", "list", "--config-root", str(config), cwd=workspace)
    assert listed.returncode == 0, listed.stderr
    result = json.loads(listed.stdout)
    assert result["proof"] == "PAL_CREATION_UNITS_LISTED"
    assert result["units"][0]["unit_id"] == "example"
    assert result["units"][0]["current_revision_id"] == first["revision_id"]

    rejected = run_pal(
        "create",
        "inspect",
        "--unit",
        "update-library",
        "--config-root",
        str(config),
        cwd=workspace,
    )
    assert rejected.returncode == 20
    error = json.loads(rejected.stderr)
    assert error["proof"] == "PAL_CREATION_INSPECT_REJECTED"
    assert "外挂库 ID" in error["error"]
    assert "example" in error["error"]
    assert "pal create list" in error["error"]
    assert tree_digest(library) == before
    assert tree_digest(config) == config_before
    assert not list(workspace.iterdir())


def test_read_only_default_binding_allows_discovery_but_rejects_mutation(
    tmp_path, monkeypatch, capsys
):
    library, config, first = initial(tmp_path)
    bind_default_creation_library(library, config_root=config)
    workspace = tmp_path / "read-only-workspace"
    workspace.mkdir()
    request = tmp_path / "new-request.json"
    write_request(request, "new-skill")
    before = tree_digest(library), tree_digest(config), tree_digest(workspace)
    monkeypatch.delenv("PAL_LIBRARY_ROOT", raising=False)
    monkeypatch.chdir(workspace)

    real_access = library_module.os.access

    def deny_development_writes(path, mode):
        if mode & os.W_OK and Path(path).resolve() == (library / "development").resolve():
            return False
        return real_access(path, mode)

    monkeypatch.setattr(library_module.os, "access", deny_development_writes)
    args = ["--config-root", str(config)]
    assert main(["create", "list", *args]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["proof"] == "PAL_CREATION_UNITS_LISTED"
    assert listed["units"][0]["current_revision_id"] == first["revision_id"]
    assert main(["create", "inspect", "--unit", "example", *args]) == 0
    assert json.loads(capsys.readouterr().out)["revision_id"] == first["revision_id"]
    assert main(["create", "inspect", "--unit", "update-library", *args]) == 20
    error = json.loads(capsys.readouterr().err)
    assert "外挂库 ID" in error["error"] and "example" in error["error"]
    assert main(["create", "begin", "--cli", "codex", "--request-file", str(request), *args]) == 20
    assert "readable and writable" in json.loads(capsys.readouterr().err)["error"]
    assert main(["create", "commit", "--creation", first["creation_id"], *args]) == 20
    assert "readable and writable" in json.loads(capsys.readouterr().err)["error"]
    assert (tree_digest(library), tree_digest(config), tree_digest(workspace)) == before


def test_concurrent_updates_reject_stale_base(tmp_path):
    library, config, _ = initial(tmp_path)
    one = update(tmp_path, library, config)
    two = update(tmp_path, library, config)
    commit_creation(library, one["creation_id"], config_root=config)
    with pytest.raises(PALError, match="已变化"):
        commit_creation(library, two["creation_id"], config_root=config)
    with pytest.raises(PALError, match="已变化"):
        begin_creation(library, "codex", tmp_path / "update.json", config_root=config)
    assert (
        inspect_creation(library, "example", config_root=config)["revision_id"]
        == one["revision_id"]
    )


@pytest.mark.parametrize("after_pointer", [False, True])
def test_update_interruption_recovers_without_overwriting_history(
    tmp_path, monkeypatch, after_pointer
):
    library, config, first = initial(tmp_path)
    opened = update(tmp_path, library, config)
    original = creation.atomic_replace_json

    def fail(path, value):
        if path.name == "unit.json":
            if after_pointer:
                original(path, value)
            raise OSError("injected interruption")
        return original(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(creation, "atomic_replace_json", fail)
        with pytest.raises(OSError, match="interruption"):
            commit_creation(library, opened["creation_id"], config_root=config)
    with pytest.raises(PALError, match="持久化"):
        abort_creation(library, opened["creation_id"])
    result = commit_creation(library, opened["creation_id"], config_root=config)
    unit = json.loads((library / "development/units/example/unit.json").read_text())
    assert unit["revision_ids"] == [first["revision_id"], result["revision_id"]]


def test_pending_update_blocks_competing_commit_and_recovers_saved_revision(tmp_path, monkeypatch):
    library, config, _ = initial(tmp_path)
    pending = update(tmp_path, library, config)
    competing = update(tmp_path, library, config)
    original = creation.atomic_replace_json

    def fail(path, value):
        if path.name == "unit.json":
            raise OSError("interruption")
        return original(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(creation, "atomic_replace_json", fail)
        with pytest.raises(OSError):
            commit_creation(library, pending["creation_id"], config_root=config)
    with pytest.raises(PALError, match="未完成的持久化"):
        commit_creation(library, competing["creation_id"], config_root=config)
    # After journaling, recovery consumes the durable revision, not editable candidates.
    Path(pending["candidates"][0]["skill_path"]).write_text("invalid edited candidate")
    commit_creation(library, pending["creation_id"], config_root=config)
    assert (
        inspect_creation(library, "example", config_root=config)["revision_id"]
        == pending["revision_id"]
    )


def test_update_rejects_candidate_symlink(tmp_path):
    library, config, first = initial(tmp_path)
    opened = update(tmp_path, library, config)
    Path(opened["candidates"][0]["skill_root"]).joinpath("escape").symlink_to(tmp_path)
    with pytest.raises(PALError):
        commit_creation(library, opened["creation_id"], config_root=config)
    assert (
        json.loads((library / "development/units/example/unit.json").read_text())[
            "current_revision_id"
        ]
        == first["revision_id"]
    )


def test_list_creation_units_reports_development_and_published_production_state(
    tmp_path, monkeypatch
):
    library, config, first = initial(tmp_path)
    listed = list_creation_units(library, config_root=config)
    assert listed["library_id"] == "update-library"
    assert listed["units"] == [
        {
            "unit_id": "example",
            "current_revision_id": first["revision_id"],
            "active_revision_id": None,
            "state": "unpublished",
        }
    ]

    active_version = "production-published"
    monkeypatch.setattr(
        creation,
        "doctor_library",
        lambda _root, **_kwargs: {
            "library_id": "update-library",
            "active_production_version_id": "production-mounted-older",
            "published_production_version_id": active_version,
        },
    )
    monkeypatch.setattr(
        "pal.publishing.validate_production_version",
        lambda _root, _version: {
            "releases": [
                {
                    "unit_id": "example",
                    "release": {"source_revision": {"revision_id": first["revision_id"]}},
                }
            ]
        },
    )
    listed = list_creation_units(library, config_root=config)
    assert listed["units"][0]["state"] == "published"
    assert listed["units"][0]["active_revision_id"] == first["revision_id"]

    opened = update(tmp_path, library, config)
    Path(opened["candidates"][0]["skill_path"]).write_text(basic_skill("example") + "\nChanged.\n")
    result = commit_creation(library, opened["creation_id"], config_root=config)
    listed = list_creation_units(library, config_root=config)
    assert listed["units"][0]["state"] == "changed"
    assert listed["units"][0]["current_revision_id"] == result["revision_id"]
    assert listed["units"][0]["active_revision_id"] == first["revision_id"]
