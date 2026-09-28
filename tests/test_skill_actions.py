"""SLC-001 through SLC-007; ACC-008/011/012: three-layer user operations."""

import json

import pytest

from pal import skill_actions
from pal.creation import begin_creation, commit_creation, list_creation_units
from pal.errors import PALError
from pal.io import sha256_file, tree_digest
from pal.maintenance import skill_action_path
from pal.publication import publish_unit, sync_production
from pal.skill_actions import (
    execute_skill_action,
    preview_skill_action,
    recover_skill_action,
)
from pal.skill_browser import skill_details
from pal.skill_state import mounted_version, read_skill_state, release_members
from pal.stages import current_production_id
from pal.web import _status
from tests.test_creation import write_request
from tests.test_production_mount import simulated_clis as simulated_clis
from tests.test_publishing import commit_unit
from tests.test_skill_updates import initial, update
from tests.test_stages import revise


def act(library, config, action, unit="example"):
    plan = preview_skill_action(library, action, unit, config_root=config)
    return execute_skill_action(library, action, unit, plan["token"], config_root=config)


def test_unmount_preserves_production_and_other_pending_changes(tmp_path, simulated_clis):
    library, config, _ = initial(tmp_path)
    publish_unit(library, "example", config_root=config, sync=True)
    commit_unit(tmp_path, library, config, "other")
    publish_unit(library, "other", config_root=config)
    published = current_production_id(library)
    act(library, config, "unmount")
    assert list_creation_units(library, config_root=config)["units"][0]["state"] == "published"
    assert current_production_id(library) == published
    assert not release_members(library, mounted_version(library))
    assert {row["unit_id"] for row in _status(library, config)["units"]} == {"example", "other"}
    revise(tmp_path, library, config, "updated while unmounted")
    result = publish_unit(library, "example", config_root=config, sync=True)
    assert not result["mount_enabled"] and "保持卸载" in result["message"]
    assert not release_members(library, mounted_version(library))
    sync_production(library, config_root=config)
    assert set(release_members(library, mounted_version(library))) == {"other"}
    assert not _status(library, config)["mount"]["sync_required"]
    act(library, config, "mount")
    assert set(release_members(library, mounted_version(library))) == {"example", "other"}
    assert not read_skill_state(library)["disabled_unit_ids"]


def test_discard_restores_complete_bundle_without_changing_production_or_mount(
    tmp_path, simulated_clis
):
    library, config, first = initial(tmp_path)
    publish_unit(library, "example", config_root=config, sync=True)
    revision_path = library / f"development/units/example/revisions/{first['revision_id']}"
    original = tree_digest(revision_path)
    revise(tmp_path, library, config, "new content")
    before = tree_digest(library / "production")
    result = act(library, config, "discard")
    assert result["revision_id"] == first["revision_id"]
    assert tree_digest(revision_path) == original
    assert tree_digest(library / "production") == before
    details = skill_details(library, "example")
    assert details["development"]["revision_id"] == details["production"]["revision_id"]
    assert any(
        file["display_path"] == "asset.bin"
        for file in details["development"]["artifacts"][0]["files"]
    )
    opened = update(tmp_path, library, config)
    assert (
        commit_creation(library, opened["creation_id"], config_root=config)["revision_id"]
        != first["revision_id"]
    )


def test_delete_stays_visible_until_explicit_unmount_then_allows_recreation(
    tmp_path, simulated_clis
):
    library, config, _ = initial(tmp_path)
    publish_unit(library, "example", config_root=config, sync=True)
    retained = {path: tree_digest(path) for path in (library / "production/versions").iterdir()}
    result = act(library, config, "delete")
    assert result["pending_deletion"] and not result["deleted"]
    assert not (library / "development/units/example").exists()
    assert not release_members(library, current_production_id(library))
    assert "example" in release_members(library, mounted_version(library))
    assert _status(library, config)["units"][0]["pending_deletion"]
    request = tmp_path / "recreate.json"
    write_request(request, "example")
    with pytest.raises(PALError, match="正在删除"):
        begin_creation(library, "codex", request, config_root=config)
    assert act(library, config, "unmount")["completed_deletion_unit_ids"] == ["example"]
    assert _status(library, config)["units"] == []
    assert (library / "production/versions").is_dir()
    assert all(path.is_dir() and tree_digest(path) == digest for path, digest in retained.items())
    assert begin_creation(library, "codex", request, config_root=config)["creation_id"]


def test_new_skill_delete_needs_no_cli_and_discard_needs_production(tmp_path):
    library, config, _ = initial(tmp_path)
    with pytest.raises(PALError, match="没有生产内容"):
        preview_skill_action(library, "discard", "example", config_root=config)
    assert act(library, config, "delete")["deleted"]
    assert _status(library, config)["units"] == []


def test_stale_preview_rejects_changes_and_open_creation_blocks_discard(tmp_path):
    library, config, _ = initial(tmp_path)
    publish_unit(library, "example", config_root=config)
    plan = preview_skill_action(library, "delete", "example", config_root=config)
    revise(tmp_path, library, config, "changed")
    with pytest.raises(PALError, match="重新预览"):
        execute_skill_action(library, "delete", "example", plan["token"], config_root=config)
    update(tmp_path, library, config)
    for action in ["delete", "discard"]:
        with pytest.raises(PALError, match="未完成创建"):
            preview_skill_action(library, action, "example", config_root=config)


def test_delete_crash_blocks_writers_and_recovers_without_unmounting(
    tmp_path, simulated_clis, monkeypatch
):
    library, config, _ = initial(tmp_path)
    publish_unit(library, "example", config_root=config, sync=True)
    mounted = sha256_file(library / "production/active.json")
    original = skill_actions.deletion._erase
    calls = 0

    def fail(path):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("interrupted cleanup")
        return original(path)

    monkeypatch.setattr(skill_actions.deletion, "_erase", fail)
    with pytest.raises(OSError, match="interrupted"):
        act(library, config, "delete")
    assert skill_action_path(library).exists()
    with pytest.raises(PALError, match="删除尚未完成"):
        publish_unit(library, "example", config_root=config)
    monkeypatch.setattr(skill_actions.deletion, "_erase", original)
    assert recover_skill_action(library, config_root=config)["pending_deletion"]
    assert sha256_file(library / "production/active.json") == mounted
    assert not skill_action_path(library).exists()
    sync_production(library, config_root=config)
    assert _status(library, config)["units"] == []


def test_unmount_failure_keeps_intent_and_allows_retry(tmp_path, simulated_clis):
    library, config, _ = initial(tmp_path)
    publish_unit(library, "example", config_root=config, sync=True)
    from pal.publishing import compose_production

    empty = compose_production(library, [])
    simulated_clis.fail_install_version = empty["production_version_id"]
    with pytest.raises(PALError):
        act(library, config, "unmount")
    row = _status(library, config)["units"][0]
    assert not row["mount_enabled"] and row["mounted_revision_id"]
    simulated_clis.fail_install_version = None
    act(library, config, "unmount")
    assert not _status(library, config)["mount"]["sync_required"]


def test_legacy_library_publication_survives_selected_mount(tmp_path, simulated_clis):
    library, config, _ = initial(tmp_path)
    published = publish_unit(library, "example", config_root=config, sync=True)
    (library / "production/current.json").unlink()
    act(library, config, "unmount")
    assert current_production_id(library) == published["production_version_id"]


def test_invalid_selection_fails_closed(tmp_path):
    library, config, _ = initial(tmp_path)
    (library / "production/skill-state.json").write_text(json.dumps({"schema_version": 20}))
    with pytest.raises(PALError):
        _status(library, config)


def test_recovery_rejects_modified_production_before_cleanup(tmp_path, monkeypatch):

    library, config, _ = initial(tmp_path)
    publish_unit(library, "example", config_root=config)
    commit_unit(tmp_path, library, config, "other")
    publish_unit(library, "other", config_root=config)
    original = skill_actions._finish_delete
    monkeypatch.setattr(
        skill_actions, "_finish_delete", lambda *_: (_ for _ in ()).throw(OSError("crash"))
    )
    with pytest.raises(OSError):
        act(library, config, "delete")
    monkeypatch.setattr(skill_actions, "_finish_delete", original)
    # Model out-of-band pointer tampering; ordinary writers are blocked by the journal.
    journal = json.loads(skill_action_path(library).read_text())
    old = current_production_id(library)
    journal["production_version_id"] = old
    skill_action_path(library).write_text(json.dumps(journal))
    with pytest.raises(PALError, match="仅移除"):
        recover_skill_action(library, config_root=config)
    assert (library / "development/units/example/unit.json").exists()
    assert current_production_id(library) == old


def test_http_and_cli_actions_bind_reviewed_scope(tmp_path, simulated_clis, capsys):
    from urllib.error import HTTPError

    from pal.cli import main
    from tests.test_web import _request, _running_server

    library, config, _ = initial(tmp_path)
    server, thread, base = _running_server(library, config)
    try:
        payload = {"action": "publish", "unit_id": "example"}
        _, plan = _request(base, "/api/skill-actions/preview", method="POST", body=payload)
        with pytest.raises(HTTPError) as rejected:
            _request(
                base,
                "/api/skill-actions/execute",
                method="POST",
                body={**payload, "token": "wrong"},
            )
        assert rejected.value.code == 409
        _, result = _request(
            base,
            "/api/skill-actions/execute",
            method="POST",
            body={**payload, "token": plan["token"]},
        )
        assert not result["synced"]
        assert mounted_version(library) is None
        assert (
            main(
                [
                    "skill",
                    "preview",
                    "--action",
                    "delete",
                    "--unit",
                    "example",
                    "--library",
                    str(library),
                    "--config-root",
                    str(config),
                ]
            )
            == 0
        )
        cli_plan = json.loads(capsys.readouterr().out)
        assert (
            main(
                [
                    "skill",
                    "apply",
                    "--action",
                    "delete",
                    "--unit",
                    "example",
                    "--token",
                    cli_plan["token"],
                    "--library",
                    str(library),
                    "--config-root",
                    str(config),
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["deleted"]
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
