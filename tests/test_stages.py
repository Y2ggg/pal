"""STG-001 through STG-008; ACC-007/008/011/012: explicit publication and sync."""

import json
from pathlib import Path

import pytest

from pal import publication
from pal.cli import main
from pal.creation import commit_creation
from pal.errors import PALError
from pal.io import sha256_file
from pal.library import doctor_library
from pal.publication import publish_unit, sync_production
from pal.publishing import validate_production_version
from pal.skill_browser import skill_details, skill_file
from pal.stages import current_production_id
from pal.web import _status
from tests.test_deletion import purge
from tests.test_production_mount import apply_action
from tests.test_production_mount import simulated_clis as simulated_clis
from tests.test_publishing import commit_unit
from tests.test_skill_updates import initial, update


def revise(tmp_path, library, config, text):
    opened = update(tmp_path, library, config)
    skill = Path(opened["candidates"][0]["skill_path"])
    skill.write_text(skill.read_text() + f"\n{text}\n")
    return commit_creation(library, opened["creation_id"], config_root=config)["revision_id"]


def test_four_stages_and_development_during_pending_sync(tmp_path, simulated_clis, monkeypatch):
    library, config, _ = initial(tmp_path)
    first = publish_unit(library, "example", config_root=config, sync=True)
    a = first["revision_id"]
    mounted_digest = sha256_file(library / "production/active.json")
    b = revise(tmp_path, library, config, "B new behavior")
    details = skill_details(library, "example")
    assert details["development"]["revision_id"] == b
    assert details["production"]["revision_id"] == details["mounted"]["revision_id"] == a
    original = publication.activate_production
    monkeypatch.setattr(
        publication, "activate_production", lambda *_a, **_k: pytest.fail("implicit sync")
    )
    published = publish_unit(library, "example", config_root=config)
    assert not published["synced"]
    assert sha256_file(library / "production/active.json") == mounted_digest
    assert _status(library, config)["mount"]["sync_required"]
    details = skill_details(library, "example")
    assert details["development"]["revision_id"] == details["production"]["revision_id"] == b
    assert details["mounted"]["revision_id"] == a
    c = revise(tmp_path, library, config, "C still unpublished")
    monkeypatch.setattr(publication, "activate_production", original)
    sync_production(library, config_root=config)
    details = skill_details(library, "example")
    assert details["development"]["revision_id"] == c
    assert details["production"]["revision_id"] == details["mounted"]["revision_id"] == b
    publish_unit(library, "example", config_root=config, sync=True)
    details = skill_details(library, "example")
    assert {source["revision_id"] for key, source in details.items() if key != "unit_id"} == {c}
    assert not _status(library, config)["mount"]["sync_required"]


def test_first_publication_never_needs_installed_clis(tmp_path, monkeypatch):
    library, config, _ = initial(tmp_path)
    monkeypatch.setattr(
        publication, "activate_production", lambda *_a, **_k: pytest.fail("implicit sync")
    )
    result = publish_unit(library, "example", config_root=config)
    doctor = doctor_library(library)
    assert doctor["published_production_version_id"] == result["production_version_id"]
    assert doctor["active_production_version_id"] is None
    assert not (config / "projections").exists()


def test_missing_development_is_visible_readable_and_not_silently_removed(tmp_path, simulated_clis):
    library, config, _ = initial(tmp_path)
    first = publish_unit(library, "example", config_root=config, sync=True)
    purge(library, config, "skill", "example")
    status = _status(library, config)
    assert status["units"][0]["state"] == "production-only"
    details = skill_details(library, "example")
    assert details["development"] is None
    source = details["production"]
    artifact = source["artifacts"][0]
    file = skill_file(
        library,
        {
            "unit_id": "example",
            "source": "production",
            "revision_id": first["revision_id"],
            "version_id": first["production_version_id"],
            "artifact_id": artifact["artifact_id"],
            "path": artifact["canonical_path"],
        },
    )
    assert file["kind"] == "markdown"
    commit_unit(tmp_path, library, config, "other")
    second = publish_unit(library, "other", config_root=config)
    assert second["preserved_unit_ids"] == ["example"]
    apply_action(library, config, "delete", "other")
    assert [
        r["unit_id"]
        for r in validate_production_version(library, current_production_id(library))["releases"]
    ] == ["example"]


def test_removal_requires_publication_then_sync(tmp_path, simulated_clis):
    library, config, _ = initial(tmp_path)
    published = publish_unit(library, "example", config_root=config, sync=True)
    apply_action(library, config, "delete")
    status = _status(library, config)
    assert status["production"]["members"] == []
    assert status["units"][0]["state"] == "mounted-only"
    assert status["mount"]["version_id"] == published["production_version_id"]
    sync_production(library, config_root=config)
    status = _status(library, config)
    assert status["units"] == []
    assert not status["mount"]["sync_required"]


def test_sync_failure_keeps_publication_and_retry_uses_it(tmp_path, simulated_clis):
    library, config, _ = initial(tmp_path)
    old = publish_unit(library, "example", config_root=config, sync=True)
    revise(tmp_path, library, config, "new")
    new = publish_unit(library, "example", config_root=config)
    simulated_clis.fail_install_version = new["production_version_id"]
    with pytest.raises(PALError, match="failed and was recovered"):
        sync_production(library, config_root=config)
    assert current_production_id(library) == new["production_version_id"]
    assert doctor_library(library)["active_production_version_id"] == old["production_version_id"]
    simulated_clis.fail_install_version = None
    sync_production(library, config_root=config)
    assert not _status(library, config)["mount"]["sync_required"]


def test_sync_rejects_stale_confirmation(tmp_path, simulated_clis):
    library, config, _ = initial(tmp_path)
    old = publish_unit(library, "example", config_root=config)
    revise(tmp_path, library, config, "new")
    publish_unit(library, "example", config_root=config)
    with pytest.raises(PALError, match="当前生产已变化"):
        sync_production(
            library, expected_version_id=old["production_version_id"], config_root=config
        )
    assert doctor_library(library)["active_production_version_id"] is None


def test_cli_manual_stages_and_rollback_rejection(tmp_path, simulated_clis, capsys):
    library, config, _ = initial(tmp_path)
    arguments = ["--library", str(library), "--config-root", str(config)]
    assert main(["publish", "--unit", "example", *arguments]) == 0
    published = json.loads(capsys.readouterr().out)
    assert not published["synced"]
    assert doctor_library(library)["active_production_version_id"] is None
    assert main(["sync", *arguments]) == 0
    assert json.loads(capsys.readouterr().out)["proof"] == "PAL_PRODUCTION_SYNCED"
    apply_action(library, config, "delete")
    assert _status(library, config)["mount"]["sync_required"]
    with pytest.raises(SystemExit) as rejected:
        main(
            ["production", "rollback", "--version", published["production_version_id"], *arguments]
        )
    assert rejected.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_current_pointer_corruption_fails_closed(tmp_path):
    library, config, _ = initial(tmp_path)
    publish_unit(library, "example", config_root=config)
    path = library / "production/current.json"
    current = json.loads(path.read_text())
    current["manifest_sha256"] = "0" * 64
    path.write_text(json.dumps(current))
    with pytest.raises(PALError, match="当前生产内容"):
        doctor_library(library)


def test_cli_and_web_sync_status_respect_stopped_and_pending_skills(
    tmp_path, simulated_clis, capsys
):
    library, config, _ = initial(tmp_path)

    def check(expected):
        assert (
            main(["production", "status", "--library", str(library), "--config-root", str(config)])
            == 0
        )
        cli = json.loads(capsys.readouterr().out)
        web = _status(library, config)
        assert cli["sync_required"] is web["mount"]["sync_required"] is expected
        assert cli["units"] == web["units"]
        return cli

    check(False)
    publish_unit(library, "example", config_root=config)
    check(True)
    sync_production(library, config_root=config)
    check(False)
    apply_action(library, config, "unmount")
    stopped = check(False)
    assert stopped["published_production_version_id"] != stopped["active_production_version_id"]
    revise(tmp_path, library, config, "New but still stopped")
    publish_unit(library, "example", config_root=config)
    check(False)
    commit_unit(tmp_path, library, config, "other")
    publish_unit(library, "other", config_root=config)
    check(True)
    sync_production(library, config_root=config)
    check(False)
    apply_action(library, config, "delete", "other")
    pending = check(True)
    assert next(u for u in pending["units"] if u["unit_id"] == "other")["pending_deletion"]
    apply_action(library, config, "unmount", "other")
    check(False)


def test_retired_production_removal_cannot_modify_library(tmp_path, capsys):
    library, config, _ = initial(tmp_path)
    from pal.io import tree_digest

    before = tree_digest(library)
    with pytest.raises(SystemExit) as rejected:
        main(["production", "remove", "--unit", "example", "--library", str(library)])
    assert rejected.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
    assert tree_digest(library) == before
