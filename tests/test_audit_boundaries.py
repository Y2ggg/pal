"""Audit 26 regressions: ACC-012; MON-004; STG-003/006; SNAP-003."""

import json
import shutil
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from pal import installation_monitor as monitor
from pal import production_mount as mounts
from pal import publishing, skill_actions, web
from pal.errors import PALError
from pal.io import formatted_json_bytes, tree_digest
from pal.maintenance import cleanup_path, skill_action_path
from pal.publication import publish_unit
from pal.publishing import compose_production, validate_production_version
from tests.test_deletion import purge
from tests.test_production_mount import apply_action
from tests.test_production_mount import simulated_clis as simulated_clis
from tests.test_skill_updates import initial
from tests.test_web import _request, _running_server


@pytest.mark.parametrize("kind", ["skill", "mounted-skill", "cleanup"])
def test_cold_web_recovers_deletion_and_keeps_writes_blocked(
    tmp_path, simulated_clis, monkeypatch, kind
):
    root, config, _ = initial(tmp_path)
    if kind == "mounted-skill":
        publish_unit(root, "example", config_root=config, sync=True)
    original = skill_actions.deletion._erase
    calls = 0

    def interrupt(path):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("injected deletion interruption")
        return original(path)

    monkeypatch.setattr(skill_actions.deletion, "_erase", interrupt)
    with pytest.raises(OSError, match="injected"):
        if kind != "cleanup":
            apply_action(root, config, "delete")
        else:
            purge(root, config, "skill", "example")
    monkeypatch.setattr(skill_actions.deletion, "_erase", original)
    journal = cleanup_path(root) if kind == "cleanup" else skill_action_path(root)
    assert journal.exists()
    server, thread, base = _running_server(root, config)
    try:
        with urlopen(base, timeout=3) as response:
            assert response.status == 200
        for path, body in [
            ("/api/status", None),
            ("/api/skill-actions/preview", {"action": "publish", "unit_id": "example"}),
        ]:
            with pytest.raises(HTTPError) as error:
                _request(base, path, method="GET" if body is None else "POST", body=body)
            assert error.value.code == 409
        assert (
            web.run_web(root, config_root=config, port=server.server_port, open_browser=False) == 0
        )
        assert _request(base, "/api/recover", method="POST", body={})[0] == 200
        assert not journal.exists()
        units = _request(base, "/api/status")[1]["units"]
        if kind == "mounted-skill":
            assert len(units) == 1 and units[0]["pending_deletion"]
            assert units[0]["mounted_revision_id"] is not None
        else:
            assert units == []
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


@pytest.mark.parametrize("cli", ["claude-code", "codex"])
def test_v1_mount_monitor_uses_original_target_version(tmp_path, simulated_clis, monkeypatch, cli):
    root, config, _ = initial(tmp_path)
    result = publish_unit(root, "example", config_root=config)
    validated = validate_production_version(root, result["production_version_id"])
    version = publishing._production_version_id(validated["releases"], 1)
    old = root / "production/versions" / version
    shutil.copytree(validated["manifest_path"].parent, old)
    (old / "production.json").write_bytes(
        formatted_json_bytes(
            {
                **validated["production"],
                "schema_version": 1,
                "production_version_id": version,
                "pal_version": "0.1.0",
            }
        )
    )
    before = tree_digest(old)
    mounts.activate_production(root, version, config_root=config)
    library_id = json.loads((root / "library.json").read_text())["library_id"]
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    _, plan = monitor._read_plan(root, config, library_id)
    source = mounts._plugin_root(plan, cli)
    manifest = (
        source / (".claude-plugin" if cli == "claude-code" else ".codex-plugin") / "plugin.json"
    )
    native_version = json.loads(manifest.read_text())["version"]
    cache = (
        monitor._cache_root(plan.marketplace_name, plan.plugin_name, native_version)
        if cli == "codex"
        else tmp_path / "claude-cache"
    )
    shutil.copytree(source, cache)
    entry = {"version": native_version, "enabled": True}
    entry.update(
        {"pluginId": mounts._plugin_id(plan), "installed": True, "source": {"path": str(source)}}
        if cli == "codex"
        else {"id": mounts._claude_plugin_id(plan), "installPath": str(cache)}
    )
    market = {
        "name": plan.marketplace_name,
        "root" if cli == "codex" else "path": str(
            mounts._marketplace_root(plan) if cli == "codex" else plan.install_roots[cli]
        ),
    }
    assert monitor._mount_status(cli, plan, [entry], [market])["state"] == "healthy"
    entry["version"] = "wrong"
    assert monitor._mount_status(cli, plan, [entry], [market])["state"] == "drift"
    assert tree_digest(old) == before


@pytest.mark.parametrize("damage", ["identity", "schema", "symlink", "library"])
def test_cold_web_rejects_invalid_recovery_context(tmp_path, monkeypatch, damage):
    root, config, _ = initial(tmp_path)

    def interrupt(*_args):
        raise OSError("injected before cleanup")

    monkeypatch.setattr(skill_actions, "_finish_delete", interrupt)
    with pytest.raises(OSError, match="injected"):
        apply_action(root, config, "delete")
    path = skill_action_path(root)
    journal = json.loads(path.read_text())
    if damage == "identity":
        journal["config_root"] = str(tmp_path / "other")
        path.write_text(json.dumps(journal))
    elif damage == "schema":
        path.write_text("{}")
    elif damage == "symlink":
        copy = tmp_path / "journal.json"
        path.rename(copy)
        path.symlink_to(copy)
    else:
        (root / "library.json").write_text("{}")
    with pytest.raises(PALError):
        web.create_server(root, config_root=config, port=0)


@pytest.fixture
def archived_publication(tmp_path, simulated_clis):
    root, config, _ = initial(tmp_path)
    published = publish_unit(root, "example", config_root=config)
    version = published["production_version_id"]
    empty = compose_production(root, [])
    from pal.stages import set_current_production

    set_current_production(root, empty["production_version_id"])
    archive = root / "records/usage/snapshots" / version
    archive.parent.mkdir(parents=True)
    shutil.move(str(root / "production/versions" / version), archive)
    return root, config, version, archive


def test_publish_reuses_archived_bytes_without_sync(archived_publication):
    root, config, version, archive = archived_publication
    before = tree_digest(archive)
    active = (root / "production/active.json").read_bytes()
    result = publish_unit(root, "example", config_root=config)
    assert result["production_version_id"] == version
    assert tree_digest(root / "production/versions" / version) == before == tree_digest(archive)
    assert (root / "production/active.json").read_bytes() == active
    assert publish_unit(root, "example", config_root=config)["production_version_id"] == version


def test_corrupt_archive_blocks_publish(archived_publication):
    root, config, version, archive = archived_publication
    next(archive.rglob("SKILL.md")).write_text("corrupt")
    before = (root / "production/current.json").read_bytes()
    with pytest.raises(PALError):
        publish_unit(root, "example", config_root=config)
    assert not (root / "production/versions" / version).exists()
    assert (root / "production/current.json").read_bytes() == before


@pytest.mark.parametrize("after_rename", [False, True])
def test_archive_republication_retries_atomically(archived_publication, monkeypatch, after_rename):
    root, config, version, archive = archived_publication
    before = tree_digest(archive)
    original = publishing.os.rename

    def interrupt(source, target):
        if target == root / "production/versions" / version:
            if after_rename:
                original(source, target)
            raise OSError("injected archive interruption")
        return original(source, target)

    monkeypatch.setattr(publishing.os, "rename", interrupt)
    with pytest.raises(OSError, match="injected"):
        publish_unit(root, "example", config_root=config)
    assert tree_digest(archive) == before
    monkeypatch.setattr(publishing.os, "rename", original)
    assert publish_unit(root, "example", config_root=config)["production_version_id"] == version
    assert tree_digest(root / "production/versions" / version) == before
