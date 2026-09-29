"""Permanent deletion, reference protection and crash recovery.

Traceability: DEL-001 through DEL-005; ACC-008, ACC-012.
"""

import json
import shutil
from pathlib import Path

import pytest

from pal import deletion, production_mount
from pal import locking as fcntl
from pal.cli import main
from pal.creation import begin_creation
from pal.errors import PALError
from pal.io import tree_digest
from pal.maintenance import cleanup_path
from pal.production_mount import activate_production
from pal.publishing import validate_production_version
from pal.usage import run_production_task, validate_usage_record
from pal.web import _status
from tests.legacy_history import (
    execute as legacy_execute,
)
from tests.legacy_history import (
    preview as legacy_preview,
)
from tests.legacy_history import (
    seed_trash as set_snapshot_deleted,
)
from tests.test_production_mount import active_version
from tests.test_production_mount import simulated_clis as simulated_clis
from tests.test_publishing import commit_unit, compose_production, create_release, setup_library
from tests.test_usage import activated_history, install_capture
from tests.test_usage import usage_clis as usage_clis


@pytest.fixture
def collection(tmp_path, simulated_clis, monkeypatch):
    root, config = setup_library(tmp_path, "deletion-test")
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_root))
    monkeypatch.setattr(production_mount, "_codex_marketplaces", lambda _: [])
    releases = {}
    for unit in ("alpha", "beta"):
        revision = commit_unit(
            tmp_path, root, config, unit, extra_files={"scripts/run.py": b"print('ok')"}
        )
        releases[unit] = create_release(root, unit, revision)["release_id"]
    old = compose_production(root, list(releases.values()))["production_version_id"]
    activate_production(root, old, config_root=config)
    active = compose_production(root, [releases["beta"]])["production_version_id"]
    activate_production(root, active, config_root=config)
    return root, config, releases, old, active


def purge(root, config, kind, object_id):
    plan = legacy_preview(root, kind, object_id, config_root=config)
    return legacy_execute(root, kind, object_id, plan["token"], config_root=config)


def test_skill_purge_keeps_immutable_history_and_shared_release(collection, simulated_clis):
    root, config, releases, old, active = collection
    before = tree_digest(root / "development/units/beta")
    snapshot_before = tree_digest(root / "production/versions" / old)
    plugin_before = simulated_clis.installed_plugin_id
    set_snapshot_deleted(root, old, deleted=True, config_root=config)
    plan = legacy_preview(root, "skill", "alpha", config_root=config)
    assert plan["versions"] == []
    assert plan["releases"] == []
    assert plan["revision_count"] == 1
    assert len(plan["creation_ids"]) == 1
    result = legacy_execute(root, "skill", "alpha", plan["token"], config_root=config)
    assert result["deleted"]
    assert not (root / "development/units/alpha").exists()
    assert (root / "releases/units/alpha" / releases["alpha"]).exists()
    assert tree_digest(root / "production/versions" / old) == snapshot_before
    assert validate_production_version(root, old)["production_version_id"] == old
    assert (config / "history/deletion-test" / f"{old}.json").exists()
    assert (config / "projections/deletion-test/versions" / old).exists()
    assert (config / "mounts/production/deletion-test/versions" / old).exists()
    for creation_id in plan["creation_ids"]:
        assert not (root / ".pal/transactions/creation" / creation_id).exists()
    assert tree_digest(root / "development/units/beta") == before
    assert (root / "releases/units/beta" / releases["beta"]).exists()
    assert simulated_clis.installed_plugin_id == plugin_before
    assert _status(root, config)["production"]["active_version_id"] == active
    set_snapshot_deleted(root, old, deleted=False, config_root=config)
    activate_production(root, old, config_root=config)
    assert _status(root, config)["production"]["active_version_id"] == old


def test_snapshot_purge_preserves_development_and_shared_release(collection):
    root, config, releases, old, _ = collection
    before = tree_digest(root / "development")
    purge(root, config, "snapshot", old)
    assert tree_digest(root / "development") == before
    assert not (root / "releases/units/alpha" / releases["alpha"]).exists()
    assert (root / "releases/units/beta" / releases["beta"]).exists()
    # Development remains publishable after its old release archive is erased.
    unit = json.loads((root / "development/units/alpha/unit.json").read_text())
    assert (
        create_release(root, "alpha", unit["current_revision_id"])["release_id"]
        == releases["alpha"]
    )
    rebuilt = compose_production(root, list(releases.values()))["production_version_id"]
    assert (root / "production/versions" / rebuilt / "production.json").exists()


def test_active_development_skill_can_be_deleted_without_touching_production(collection):
    root, config, _, _, active = collection
    plan = legacy_preview(root, "skill", "beta", config_root=config)
    assert plan["versions"] == []
    result = legacy_execute(root, "skill", "beta", plan["token"], config_root=config)
    assert result["deleted"]
    assert not (root / "development/units/beta").exists()
    assert validate_production_version(root, active)["production_version_id"] == active


def test_active_snapshot_is_protected(collection):
    root, config, _, _, active = collection
    with pytest.raises(PALError, match="当前生产"):
        legacy_preview(root, "snapshot", active, config_root=config)
    assert not cleanup_path(root).exists()


def test_pending_usage_blocks_snapshot_deletion(collection):
    root, config, _, old, _ = collection
    record = root / ".pal/transactions/usage/pending/context.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"production_version_id": old}))
    with pytest.raises(PALError, match="未完成使用事务"):
        purge(root, config, "snapshot", old)
    assert record.exists() and (root / "production/versions" / old).exists()


@pytest.mark.skipif(shutil.which("codex") is None, reason="requires an installed Codex CLI")
def test_recorded_snapshot_is_removed_from_history_but_remains_verifiable(
    tmp_path, usage_clis, monkeypatch
):
    history = activated_history(tmp_path)
    root, config, old = history["library"], history["config"], history["v1"]
    install_capture(monkeypatch, "succeeded")
    result = run_production_task(
        root, "claude-code", "Use the published Skill.", config_root=config
    )
    record = Path(result["record"])
    activate_production(root, history["v2"], config_root=config)
    plan = legacy_preview(root, "snapshot", old, config_root=config)
    assert plan["versions"][0]["archive_required"]
    assert plan["usage_references"] == [record.relative_to(root).as_posix()]
    purge(root, config, "snapshot", old)
    assert not (root / "production/versions" / old).exists()
    assert (root / "records/usage/snapshots" / old / "production.json").exists()
    assert validate_usage_record(root, record, config_root=config)["production_version_id"] == old
    assert active_version(history) == history["v2"]
    with pytest.raises(PALError, match="旧证据归档"):
        activate_production(root, old, config_root=config)
    archive = root / "records/usage/snapshots" / old
    archive_digest = tree_digest(archive)
    record_bytes = record.read_bytes()
    compose_production(root, [validate_production_version(root, old)["releases"][0]["release_id"]])
    assert tree_digest(root / "production/versions" / old) == archive_digest
    assert tree_digest(archive) == archive_digest
    assert record.read_bytes() == record_bytes
    assert validate_usage_record(root, record, config_root=config)["production_version_id"] == old
    # The historical deletion fixture remains readable after explicit republication.
    purge(root, config, "snapshot", old)
    empty = compose_production(root, [])["production_version_id"]
    activate_production(root, empty, config_root=config)
    skill_plan = legacy_preview(root, "skill", "first-mounted-skill", config_root=config)
    assert skill_plan["versions"] == []
    assert skill_plan["releases"] == []
    purge(root, config, "skill", "first-mounted-skill")
    assert not (root / "development/units/first-mounted-skill").exists()
    assert validate_usage_record(root, record, config_root=config)["production_version_id"] == old


@pytest.mark.skipif(shutil.which("codex") is None, reason="requires an installed Codex CLI")
def test_archived_snapshot_cleanup_recovers_after_interruption(tmp_path, usage_clis, monkeypatch):
    history = activated_history(tmp_path)
    root, config, old = history["library"], history["config"], history["v1"]
    install_capture(monkeypatch, "succeeded")
    record = Path(
        run_production_task(root, "claude-code", "Use the published Skill.", config_root=config)[
            "record"
        ]
    )
    activate_production(root, history["v2"], config_root=config)
    original = deletion._erase
    interrupted = False

    def interrupt(path):
        nonlocal interrupted
        if path == root / "production/versions" / old and not interrupted:
            interrupted = True
            assert (root / "records/usage/snapshots" / old).exists()
            raise OSError("injected archive cleanup interruption")
        return original(path)

    monkeypatch.setattr(deletion, "_erase", interrupt)
    with pytest.raises(OSError, match="archive cleanup interruption"):
        purge(root, config, "snapshot", old)
    assert cleanup_path(root).exists()
    monkeypatch.setattr(deletion, "_erase", original)
    assert deletion.recover_deletion(root, config_root=config)["deleted"]
    assert not cleanup_path(root).exists()
    assert validate_usage_record(root, record, config_root=config)["production_version_id"] == old


def test_stale_preview_rejected_after_history_change(collection):
    root, config, _, old, _ = collection
    plan = legacy_preview(root, "snapshot", old, config_root=config)
    set_snapshot_deleted(root, old, deleted=True, config_root=config)
    with pytest.raises(PALError, match="重新预览"):
        legacy_execute(root, "snapshot", old, plan["token"], config_root=config)
    assert (root / "production/versions" / old).exists()


def test_cleanup_journal_without_archive_fields_remains_recoverable(collection, monkeypatch):
    root, config, _, old, _ = collection
    original = deletion._finish

    def interrupt(*_):
        raise OSError("legacy cleanup interruption")

    monkeypatch.setattr(deletion, "_finish", interrupt)
    with pytest.raises(OSError, match="legacy cleanup interruption"):
        purge(root, config, "snapshot", old)
    journal = json.loads(cleanup_path(root).read_text())
    journal["plan"].pop("usage_references")
    for version in journal["plan"]["versions"]:
        for field in ("archive_required", "archive_path", "tree_sha256"):
            version.pop(field)
    cleanup_path(root).write_text(json.dumps(journal))
    monkeypatch.setattr(deletion, "_finish", original)
    assert deletion.recover_deletion(root, config_root=config)["deleted"]
    assert not (root / "production/versions" / old).exists()
    assert not cleanup_path(root).exists()


def test_open_update_blocks_skill_deletion(collection, tmp_path):
    root, config, _, _, _ = collection
    unit = json.loads((root / "development/units/alpha/unit.json").read_text())
    request = json.loads((tmp_path / "alpha.request.json").read_text())
    request["schema_version"] = 2
    request["base_revision_id"] = unit["current_revision_id"]
    path = tmp_path / "update.json"
    path.write_text(json.dumps(request))
    opened = begin_creation(root, "codex", path, config_root=config)
    with pytest.raises(PALError, match=opened["creation_id"]):
        purge(root, config, "skill", "alpha")


@pytest.mark.parametrize("stage", ["creation", "development"])
def test_interrupted_cleanup_blocks_writes_and_resumes(collection, monkeypatch, stage):
    root, config, releases, _, _ = collection
    original = deletion._erase
    failed = False

    def interrupt(path):
        nonlocal failed
        target = {
            "production": "production/versions",
            "creation": ".pal/transactions/creation",
            "development": "development/units/alpha",
        }[stage]
        if target in str(path) and path.is_file() and not failed:
            original(path)
            failed = True
            raise OSError("injected cleanup interruption")
        return original(path)

    monkeypatch.setattr(deletion, "_erase", interrupt)
    with pytest.raises(OSError, match="interruption"):
        purge(root, config, "skill", "alpha")
    assert cleanup_path(root).exists()
    with pytest.raises(PALError, match="永久删除尚未完成"):
        compose_production(root, [releases["beta"]])
    with pytest.raises(PALError, match="永久删除尚未完成"):
        _status(root, config)
    monkeypatch.setattr(deletion, "_erase", original)
    if stage == "creation":
        assert main(["recover", "--library", str(root), "--config-root", str(config)]) == 0
    else:
        assert deletion.recover_deletion(root, config_root=config)["deleted"]
    assert not cleanup_path(root).exists()
    assert [unit["unit_id"] for unit in _status(root, config)["units"]] == ["beta"]
    assert not deletion.recover_deletion(root, config_root=config)["deleted"]


def test_symbolic_link_is_rejected_without_touching_outside(collection, tmp_path):
    root, config, _, _, _ = collection
    outside = tmp_path / "outside"
    outside.write_text("keep")
    (root / "development/units/alpha/escape").symlink_to(outside)
    with pytest.raises(PALError, match="symbolic link"):
        purge(root, config, "skill", "alpha")
    assert outside.read_text() == "keep"


def test_maintenance_lock_rejects_concurrent_cleanup(collection):
    root, config, _, old, _ = collection
    with (root / ".pal/locks/maintenance.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with pytest.raises(PALError, match="正在执行其他操作"):
            purge(root, config, "snapshot", old)


def test_never_published_skill_does_not_require_cli(tmp_path, monkeypatch):
    root, config = setup_library(tmp_path, "unreleased-delete")
    commit_unit(tmp_path, root, config, "alpha")
    monkeypatch.setattr(
        deletion, "_detach_snapshot", lambda *_: pytest.fail("no CLI cleanup needed")
    )
    purge(root, config, "skill", "alpha")
    assert _status(root, config)["units"] == []


@pytest.mark.parametrize("interrupted", [False, True])
def test_owned_claude_cache_is_removed_and_other_cache_preserved(
    collection, monkeypatch, interrupted
):
    root, config, _, old, _ = collection
    version = legacy_preview(root, "snapshot", old, config_root=config)["versions"][0]
    plugin, _ = production_mount._projection_identity("deletion-test", old, stable=True)
    parent = deletion._cache_parent(deletion._claude_root(), "deletion-test", version)
    cached = parent / "0.1.0-test"
    source = config / "projections/deletion-test/versions" / old / "claude-code/plugins" / plugin
    shutil.copytree(source, cached)
    (cached / ".orphaned_at").write_text("1790136000000")
    other = deletion._claude_root() / "plugins/cache/other-plugin/keep.txt"
    other.parent.mkdir(parents=True)
    other.write_text("keep")
    original = deletion._erase
    if interrupted:

        def fail(path):
            if "plugins/cache" in str(path) and path.is_file():
                original(path)
                raise OSError("cache interruption")
            original(path)

        monkeypatch.setattr(deletion, "_erase", fail)
        with pytest.raises(OSError, match="cache interruption"):
            purge(root, config, "snapshot", old)
        monkeypatch.setattr(deletion, "_erase", original)
        deletion.recover_deletion(root, config_root=config)
    else:
        purge(root, config, "snapshot", old)
    assert not cached.exists()
    assert other.read_text() == "keep"


def test_modified_claude_cache_blocks_deletion(collection):
    root, config, _, old, _ = collection
    version = legacy_preview(root, "snapshot", old, config_root=config)["versions"][0]
    cached = (
        deletion._cache_parent(deletion._claude_root(), "deletion-test", version) / "0.1.0-test"
    )
    cached.mkdir(parents=True)
    (cached / "personal.txt").write_text("not an PAL payload")
    with pytest.raises(PALError):
        purge(root, config, "snapshot", old)
    assert (cached / "personal.txt").exists()


def test_malformed_kind_is_rejected_and_keyword_call_contract_preserved(tmp_path):
    root, config = setup_library(tmp_path, "bad-delete")
    assert _status(root=root, config_root=config)["units"] == []
    with pytest.raises(ValueError, match="删除类型"):
        legacy_preview(root, [], "anything", config_root=config)


def test_new_snapshot_cleanup_cannot_be_started(collection):
    root, config, _, old, _ = collection
    with pytest.raises(ValueError, match="历史快照管理已退出"):
        deletion.preview_deletion(root, "snapshot", old, config_root=config)
