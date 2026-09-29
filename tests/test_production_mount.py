"""Production mount, activation, rollback, and recovery tests.

Traceability: PRD-MOUNT-003, PRD-MOUNT-004, PRD-RELEASE-002;
ACC-008, ACC-011, ACC-012.
"""

from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import pal.cli as cli_module
import pal.production_mount as production_mount_module
from pal import locking as fcntl
from pal.adapters import launch_creation_entry
from pal.compatibility import CliCompatibility
from pal.errors import CreationError, PALError, ProductionError
from pal.io import sha256_file, tree_digest
from pal.production_mount import (
    activate_production,
    launch_production_entry,
    recover_production,
    validate_production_mount,
)
from pal.publication import (
    migrate_production_namespace,
    publish_unit,
)
from pal.publishing import validate_production_version
from pal.skill_actions import execute_skill_action, preview_skill_action
from pal.skill_browser import skill_details, skill_file
from tests.test_publishing import (
    commit_unit,
    compose_production,
    create_release,
    development_skill,
    setup_library,
    skill_text,
)


class SimulatedTarget:
    """One target's persistent-installation state for transition semantics tests."""

    def __init__(self, cli_id: str) -> None:
        self.cli_id = cli_id
        self.installed_plugin_id: str | None = None
        self.fail_install_version: str | None = None

    def plugin_id(self, plan: Any) -> str:
        if self.cli_id == "codex":
            return f"{plan.plugin_name}@{plan.marketplace_name}"
        return f"{plan.plugin_name}@{plan.marketplace_name}"

    def install(self, plan: Any) -> None:
        if self.fail_install_version == plan.version_id:
            raise ProductionError(f"injected {self.cli_id} installation failure")
        self.installed_plugin_id = self.plugin_id(plan)

    def remove(self, plan: Any) -> None:
        if self.installed_plugin_id == self.plugin_id(plan):
            self.installed_plugin_id = None

    def verify(self, plan: Any) -> dict[str, Any]:
        if self.installed_plugin_id != self.plugin_id(plan):
            raise ProductionError(f"simulated {self.cli_id} production plugin is not installed")
        if self.cli_id == "codex":
            return {"pluginId": self.installed_plugin_id, "installed": True}
        return {"id": self.installed_plugin_id, "enabled": True}


class SimulatedCodex:
    """Both simulated targets, exposing the Codex knobs the tests already use."""

    def __init__(self) -> None:
        self.codex = SimulatedTarget("codex")
        self.claude = SimulatedTarget("claude-code")

    @property
    def installed_plugin_id(self) -> str | None:
        return self.codex.installed_plugin_id

    @property
    def fail_install_version(self) -> str | None:
        return self.codex.fail_install_version

    @fail_install_version.setter
    def fail_install_version(self, value: str | None) -> None:
        # Injected failures target Codex only, so Claude stays a healthy peer and
        # the assertions keep observing one failing target at a time.
        self.codex.fail_install_version = value

    def plugin_id(self, plan: Any) -> str:
        return self.codex.plugin_id(plan)


def apply_action(library, config, action, unit="example"):
    plan = preview_skill_action(library, action, unit, config_root=config)
    return execute_skill_action(library, action, unit, plan["token"], config_root=config)


def compatible_targets() -> dict[str, CliCompatibility]:
    return {
        "claude-code": CliCompatibility(
            "claude-code",
            "claude",
            "2.1.234",
            "2.1.205",
            "verified-version",
            (),
        ),
        "codex": CliCompatibility(
            "codex",
            "codex",
            "0.147.0",
            "0.147.0",
            "verified-version",
            (),
        ),
    }


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


@pytest.mark.parametrize("directed", [False, True])
def test_multifile_publish_projects_all_files_and_preserves_old_snapshot(
    tmp_path: Path, simulated_clis: SimulatedCodex, directed: bool
) -> None:
    library, config = setup_library(tmp_path, "multifile-production")
    commit_unit(tmp_path, library, config, "legacy")
    legacy = publish_unit(library, "legacy", config_root=config, sync=True)
    old_version = legacy["production_version_id"]
    old_root = library / f"production/versions/{old_version}"
    old_digest = tree_digest(old_root)
    extras = {
        "references/guide.txt": b"reference-token\n",
        "scripts/read.py": (
            b"from pathlib import Path\n"
            b"print(Path(__file__).parent.parent.joinpath('references/guide.txt').read_text().strip())\n"
        ),
        "assets/template.bin": b"\x00\xff\x80",
    }
    revision = commit_unit(
        tmp_path, library, config, "files", directed=directed, extra_files=extras
    )
    published = publish_unit(library, "files", config_root=config, sync=True)
    version = published["production_version_id"]
    projection = config / f"projections/multifile-production/versions/{version}"
    for cli_id in ("claude-code", "codex"):
        skills = list((projection / cli_id).rglob("skills/files/SKILL.md"))
        assert len(skills) == 1
        for relative, content in extras.items():
            assert (skills[0].parent / relative).read_bytes() == content
    details = skill_details(library, "files")
    production = details["production"]
    artifact = production["artifacts"][0]
    preview = skill_file(
        library,
        {
            "unit_id": "files",
            "source": "production",
            "revision_id": revision,
            "version_id": version,
            "artifact_id": artifact["artifact_id"],
            "path": "skills/files/references/guide.txt",
        },
    )
    assert preview["content"] == "reference-token\n"
    development = library / f"development/units/files/revisions/{revision}"
    next(development.rglob("references/guide.txt")).write_bytes(b"unpublished change")
    assert validate_production_version(library, version)["production_version_id"] == version
    assert tree_digest(old_root) == old_digest
    activate_production(library, old_version, config_root=config)
    assert tree_digest(old_root) == old_digest
    activate_production(library, version, config_root=config)
    attachment = next((library / f"production/versions/{version}").rglob("references/guide.txt"))
    attachment.unlink()
    with pytest.raises(PALError):
        validate_production_version(library, version)


def production_history(tmp_path: Path) -> dict[str, Any]:
    library_root, config_root = setup_library(tmp_path, "production-mount-main")
    first_revision = commit_unit(tmp_path, library_root, config_root, "first-mounted-skill")
    first_release = create_release(library_root, "first-mounted-skill", first_revision)
    first_production = compose_production(library_root, [first_release["release_id"]])

    second_revision = commit_unit(tmp_path, library_root, config_root, "second-mounted-skill")
    second_release = create_release(library_root, "second-mounted-skill", second_revision)
    second_production = compose_production(
        library_root,
        [first_release["release_id"], second_release["release_id"]],
    )
    return {
        "library": library_root,
        "config": config_root,
        "first_revision": first_revision,
        "v1": first_production["production_version_id"],
        "v2": second_production["production_version_id"],
    }


def transition_path(history: dict[str, Any]) -> Path:
    return history["config"] / "transitions/production/production-mount-main.json"


def active_version(history: dict[str, Any]) -> str | None:
    value = json.loads((history["library"] / "production/active.json").read_text(encoding="utf-8"))
    return value["active_production_version_id"]


def test_acc_008_first_activation_materializes_two_cli_mounts(
    tmp_path: Path,
    simulated_clis: SimulatedCodex,
) -> None:
    history = production_history(tmp_path)
    result = activate_production(
        history["library"],
        history["v1"],
        config_root=history["config"],
    )

    assert result["idempotent"] is False
    assert result["previous_production_version_id"] is None
    assert result["effective_on"] == "new-session"
    assert active_version(history) == history["v1"]
    assert simulated_clis.installed_plugin_id == (
        f"{result['plugin_name']}@{result['marketplace_name']}"
    )
    validated = validate_production_mount(
        history["library"], history["v1"], config_root=history["config"]
    )
    bundle = json.loads(Path(validated["bundle"]).read_text(encoding="utf-8"))
    assert [item["cli_id"] for item in bundle["cli_records"]] == ["claude-code", "codex"]
    for cli_id in ("claude-code", "codex"):
        record = json.loads(
            (Path(validated["bundle"]).parent / f"{cli_id}.json").read_text(encoding="utf-8")
        )
        assert record["cli_version"] in {"2.1.234", "0.147.0"}
        assert record["projection_root"] == validated["projection_roots"][cli_id]
        assert "development/" not in json.dumps(record, ensure_ascii=False)
    assert not transition_path(history).exists()

    repeated = activate_production(history["library"], history["v1"], config_root=history["config"])
    assert repeated["idempotent"] is True
    assert repeated["reused_projection"] is True
    assert repeated["reused_mount"] is True


def test_acc_008_switch_and_rollback_retain_versioned_projections(
    tmp_path: Path,
    simulated_clis: SimulatedCodex,
) -> None:
    history = production_history(tmp_path)
    first = activate_production(history["library"], history["v1"], config_root=history["config"])
    v1_projection = history["config"] / (
        f"projections/production-mount-main/versions/{history['v1']}"
    )
    v1_digest = tree_digest(v1_projection)

    second = activate_production(history["library"], history["v2"], config_root=history["config"])
    assert second["previous_production_version_id"] == history["v1"]
    assert active_version(history) == history["v2"]
    assert tree_digest(v1_projection) == v1_digest
    assert simulated_clis.installed_plugin_id != (
        f"{first['plugin_name']}@{first['marketplace_name']}"
    )

    rolled_back = activate_production(
        history["library"], history["v1"], config_root=history["config"]
    )
    assert rolled_back["production_version_id"] == history["v1"]
    assert active_version(history) == history["v1"]
    assert simulated_clis.installed_plugin_id == (
        f"{first['plugin_name']}@{first['marketplace_name']}"
    )
    assert tree_digest(v1_projection) == v1_digest


def test_acc_011_development_drift_does_not_change_active_mount_or_projection(
    tmp_path: Path,
    simulated_clis: SimulatedCodex,
) -> None:
    history = production_history(tmp_path)
    activated = activate_production(
        history["library"], history["v1"], config_root=history["config"]
    )
    bundle = Path(activated["bundle"])
    projection = history["config"] / (f"projections/production-mount-main/versions/{history['v1']}")
    active_path = history["library"] / "production/active.json"
    before = (sha256_file(bundle), tree_digest(projection), sha256_file(active_path))

    development_skill(
        history["library"], "first-mounted-skill", history["first_revision"]
    ).write_text(
        skill_text("first-mounted-skill") + "\nDevelopment-only drift.\n",
        encoding="utf-8",
    )

    validated = validate_production_mount(
        history["library"], history["v1"], config_root=history["config"]
    )
    assert validated["bundle_sha256"] == before[0]
    assert (sha256_file(bundle), tree_digest(projection), sha256_file(active_path)) == before


def test_acc_012_projection_drift_blocks_rollback_without_changing_active(
    tmp_path: Path,
    simulated_clis: SimulatedCodex,
) -> None:
    history = production_history(tmp_path)
    activate_production(history["library"], history["v1"], config_root=history["config"])
    activate_production(history["library"], history["v2"], config_root=history["config"])
    active_before = (history["library"] / "production/active.json").read_bytes()
    skill = next(
        (history["config"] / f"projections/production-mount-main/versions/{history['v1']}").glob(
            "**/SKILL.md"
        )
    )
    skill.write_text(skill.read_text(encoding="utf-8") + "\nTampered.\n", encoding="utf-8")

    with pytest.raises(PALError, match="drift|inventory|SHA-256"):
        activate_production(history["library"], history["v1"], config_root=history["config"])

    assert (history["library"] / "production/active.json").read_bytes() == active_before
    assert transition_path(history).is_file()
    with pytest.raises(PALError, match="drift|inventory|SHA-256"):
        recover_production(history["library"], config_root=history["config"])
    assert transition_path(history).is_file()


@pytest.mark.parametrize("cli_id", ["claude-code", "codex"])
def test_acc_012_target_install_failure_restores_both_plugins_and_pointer(
    tmp_path: Path,
    simulated_clis: SimulatedCodex,
    cli_id: str,
) -> None:
    history = production_history(tmp_path)
    first = activate_production(history["library"], history["v1"], config_root=history["config"])
    active_before = (history["library"] / "production/active.json").read_bytes()
    failing_target = simulated_clis.claude if cli_id == "claude-code" else simulated_clis.codex
    failing_target.fail_install_version = history["v2"]

    with pytest.raises(PALError, match="failed and was recovered"):
        activate_production(history["library"], history["v2"], config_root=history["config"])

    assert (history["library"] / "production/active.json").read_bytes() == active_before
    assert simulated_clis.installed_plugin_id == (
        f"{first['plugin_name']}@{first['marketplace_name']}"
    )
    assert simulated_clis.claude.installed_plugin_id == (
        f"{first['plugin_name']}@{first['marketplace_name']}"
    )
    assert not transition_path(history).exists()


class SimulatedCrash(BaseException):
    """Represents process loss, which normal exception recovery cannot intercept."""


def test_acc_012_explicit_recovery_restores_old_for_precommit_crash(
    tmp_path: Path,
    simulated_clis: SimulatedCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = production_history(tmp_path)
    first = activate_production(history["library"], history["v1"], config_root=history["config"])
    active_before = (history["library"] / "production/active.json").read_bytes()
    real_materialize = production_mount_module._materialize_plan

    def crash(_plan: Any) -> tuple[bool, bool]:
        raise SimulatedCrash("injected precommit process loss")

    monkeypatch.setattr(production_mount_module, "_materialize_plan", crash)
    with pytest.raises(SimulatedCrash):
        activate_production(history["library"], history["v2"], config_root=history["config"])
    assert transition_path(history).is_file()

    monkeypatch.setattr(production_mount_module, "_materialize_plan", real_materialize)
    recovered = recover_production(history["library"], config_root=history["config"])
    assert recovered["recovery_action"] == "restored-old"
    assert (history["library"] / "production/active.json").read_bytes() == active_before
    assert simulated_clis.installed_plugin_id == (
        f"{first['plugin_name']}@{first['marketplace_name']}"
    )
    assert not transition_path(history).exists()


def test_acc_012_explicit_recovery_completes_target_after_commit_window(
    tmp_path: Path,
    simulated_clis: SimulatedCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = production_history(tmp_path)
    activate_production(history["library"], history["v1"], config_root=history["config"])
    real_advance = production_mount_module._advance_transition

    def crash_on_committed(
        path: Path,
        transition: dict[str, Any],
        phase: str,
        **keywords: Any,
    ) -> None:
        if phase == "ACTIVE_POINTER_COMMITTED":
            raise SimulatedCrash("injected post-pointer process loss")
        real_advance(path, transition, phase, **keywords)

    monkeypatch.setattr(production_mount_module, "_advance_transition", crash_on_committed)
    with pytest.raises(SimulatedCrash):
        activate_production(history["library"], history["v2"], config_root=history["config"])
    assert active_version(history) == history["v2"]
    assert transition_path(history).is_file()

    monkeypatch.setattr(production_mount_module, "_advance_transition", real_advance)
    recovered = recover_production(history["library"], config_root=history["config"])
    assert recovered["recovery_action"] == "completed-target"
    assert active_version(history) == history["v2"]
    assert not transition_path(history).exists()


def test_acc_012_drift_during_recovery_preserves_transition(
    tmp_path: Path,
    simulated_clis: SimulatedCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = production_history(tmp_path)
    activate_production(history["library"], history["v1"], config_root=history["config"])
    real_materialize = production_mount_module._materialize_plan
    monkeypatch.setattr(
        production_mount_module,
        "_materialize_plan",
        lambda _plan: (_ for _ in ()).throw(SimulatedCrash()),
    )
    with pytest.raises(SimulatedCrash):
        activate_production(history["library"], history["v2"], config_root=history["config"])
    monkeypatch.setattr(production_mount_module, "_materialize_plan", real_materialize)
    production_skill = next(
        (history["library"] / f"production/versions/{history['v2']}/payload").glob("**/SKILL.md")
    )
    production_skill.write_text(
        production_skill.read_text(encoding="utf-8") + "\nTampered.\n",
        encoding="utf-8",
    )

    with pytest.raises(PALError, match="SHA-256|inventory|tree"):
        recover_production(history["library"], config_root=history["config"])
    assert transition_path(history).is_file()
    assert active_version(history) == history["v1"]


def test_acc_012_transition_blocks_creation_entry(
    tmp_path: Path,
    simulated_clis: SimulatedCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = production_history(tmp_path)
    monkeypatch.setattr(
        production_mount_module,
        "_materialize_plan",
        lambda _plan: (_ for _ in ()).throw(SimulatedCrash()),
    )
    with pytest.raises(SimulatedCrash):
        activate_production(history["library"], history["v1"], config_root=history["config"])

    with pytest.raises(CreationError, match="during a production transition"):
        launch_creation_entry(
            history["library"],
            "codex",
            config_root=history["config"],
        )


def test_acc_012_activation_lock_and_cli_version_gate_fail_closed(
    tmp_path: Path,
    simulated_clis: SimulatedCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = production_history(tmp_path)
    lock_path = history["config"] / "locks/production-mount-main.activation.lock"
    lock_path.parent.mkdir(exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(PALError, match="locked"):
            activate_production(history["library"], history["v1"], config_root=history["config"])
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    monkeypatch.setattr(
        production_mount_module,
        "_detect_target_compatibilities",
        lambda: (_ for _ in ()).throw(ProductionError("unsupported codex version")),
    )
    with pytest.raises(PALError, match="unsupported codex version"):
        activate_production(history["library"], history["v1"], config_root=history["config"])
    assert active_version(history) is None
    assert not transition_path(history).exists()


def test_publish_unit_preserves_active_units_and_is_idempotent(
    tmp_path: Path,
    simulated_clis: SimulatedCodex,
) -> None:
    library, config = setup_library(tmp_path, "guided-publish")
    first_revision = commit_unit(tmp_path, library, config, "first-published-skill")

    first = publish_unit(library, "first-published-skill", config_root=config, sync=True)

    assert first["revision_id"] == first_revision
    assert first["preserved_unit_ids"] == []
    assert first["previous_production_version_id"] is None
    assert first["invocations"] == {
        "claude-code": f"{first['plugin_name']}:first-published-skill",
        "codex": f"${first['plugin_name']}:first-published-skill",
    }
    assert active_version({"library": library}) == first["production_version_id"]

    second_revision = commit_unit(tmp_path, library, config, "second-published-skill")
    second = publish_unit(library, "second-published-skill", config_root=config, sync=True)

    assert second["revision_id"] == second_revision
    assert second["preserved_unit_ids"] == ["first-published-skill"]
    active = validate_production_version(library, second["production_version_id"])
    assert {item["unit_id"]: item["release_id"] for item in active["releases"]} == {
        "first-published-skill": first["release_id"],
        "second-published-skill": second["release_id"],
    }

    repeated = publish_unit(library, "second-published-skill", config_root=config, sync=True)
    assert repeated["release_idempotent"] is True
    assert repeated["production_idempotent"] is True
    assert repeated["activation_idempotent"] is True
    assert repeated["production_version_id"] == second["production_version_id"]


@pytest.mark.parametrize("cli", ["codex", "claude-code"])
def test_stable_namespace_collision_is_rejected_before_install(tmp_path, monkeypatch, cli):
    from types import SimpleNamespace

    plan = SimpleNamespace(
        plugin_name="pal-example", marketplace_name="owned-market", executables={cli: cli}
    )
    if cli == "codex":
        monkeypatch.setattr(
            production_mount_module,
            "_codex_plugins",
            lambda _: [
                {"name": "pal-example", "pluginId": "pal-example@foreign", "installed": True}
            ],
        )
        install = production_mount_module._install_codex
    else:
        monkeypatch.setattr(
            production_mount_module,
            "_claude_plugins",
            lambda _: [{"id": "pal-example@foreign", "enabled": True}],
        )
        install = production_mount_module._install_claude
    with pytest.raises(ProductionError, match="同名插件"):
        install(plan)


@pytest.mark.parametrize("library_id", ["normal-library", "with.dots_and_underscores", "a" * 80])
def test_stable_namespace_is_legal_and_independent_of_version(library_id):
    import re

    one, _ = production_mount_module._projection_identity(library_id, "one", stable=True)
    two, _ = production_mount_module._projection_identity(library_id, "two", stable=True)
    assert one == two
    assert len(one) <= 52
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", one)


def test_unmount_all_preserves_content_and_supports_remount(tmp_path, simulated_clis):
    library, config = setup_library(tmp_path, "empty-set")
    commit_unit(tmp_path, library, config, "example")
    first = publish_unit(library, "example", config_root=config, sync=True)
    before = tree_digest(library / "development")
    empty = apply_action(library, config, "unmount")
    version = validate_production_version(library, empty["production_version_id"])
    assert version["production"]["releases"] == version["production"]["files"] == []
    mount = validate_production_mount(library, empty["production_version_id"], config_root=config)
    assert mount["plugin_name"] == first["plugin_name"] == "pal-empty-set"
    for root in mount["projection_roots"].values():
        assert not list(Path(root).rglob("SKILL.md"))
    apply_action(library, config, "mount")
    assert tree_digest(library / "development") == before
    assert active_version({"library": library}) == first["production_version_id"]


def test_empty_set_install_failure_restores_previous_production(tmp_path, simulated_clis):
    library, config = setup_library(tmp_path, "empty-failure")
    commit_unit(tmp_path, library, config, "example")
    first = publish_unit(library, "example", config_root=config, sync=True)
    empty = compose_production(library, [])
    simulated_clis.fail_install_version = empty["production_version_id"]
    with pytest.raises(ProductionError, match="recovered"):
        apply_action(library, config, "unmount")
    assert active_version({"library": library}) == first["production_version_id"]
    mount = validate_production_mount(library, first["production_version_id"], config_root=config)
    assert (
        simulated_clis.codex.installed_plugin_id
        == simulated_clis.claude.installed_plugin_id
        == f"{mount['plugin_name']}@{mount['marketplace_name']}"
    )


def test_v1_namespace_migration_preserves_old_bytes_and_active_releases(tmp_path, simulated_clis):
    from pal.io import formatted_json_bytes
    from pal.publishing import _production_version_id

    library, config = setup_library(tmp_path, "legacy-migration")
    commit_unit(tmp_path, library, config, "example")
    new = publish_unit(library, "example", config_root=config, sync=True)
    validated = validate_production_version(library, new["production_version_id"])
    old_id = _production_version_id(validated["releases"], 1)
    old_root = library / "production/versions" / old_id
    shutil.copytree(validated["manifest_path"].parent, old_root)
    old_manifest = {**validated["production"], "schema_version": 1, "production_version_id": old_id}
    (old_root / "production.json").write_bytes(formatted_json_bytes(old_manifest))
    old = activate_production(library, old_id, config_root=config)
    old_mount = validate_production_mount(library, old_id, config_root=config)
    before = tree_digest(old_root)
    projection_before = tree_digest(Path(old_mount["projection_roots"]["codex"]).parent)
    commit_unit(tmp_path, library, config, "unpublished")
    migrated = migrate_production_namespace(library, config_root=config, sync=True)
    assert migrated["plugin_name"] == "pal-legacy-migration"
    assert old["plugin_name"].startswith("pal-production-")
    assert migrated["production_version_id"] == new["production_version_id"]
    assert validate_production_mount(library, old_id, config_root=config) == old_mount
    assert tree_digest(old_root) == before
    assert tree_digest(Path(old_mount["projection_roots"]["codex"]).parent) == projection_before
    activate_production(library, old_id, config_root=config)
    assert active_version({"library": library}) == old_id
    assert (
        migrate_production_namespace(library, config_root=config, sync=True)["plugin_name"]
        == "pal-legacy-migration"
    )


def test_claude_persistent_install_root_stays_outside_the_immutable_projection(
    tmp_path: Path,
    simulated_clis: SimulatedCodex,
) -> None:
    """Claude gets a real marketplace root without rewriting projection bytes.

    Claude resolves a plugin ``source`` relative to the marketplace root, so the
    manifest cannot live outside it.  Keeping the root beside the projection lets
    older versions stay byte-identical and therefore remain activatable.
    """

    history = production_history(tmp_path)
    activate_production(history["library"], history["v1"], config_root=history["config"])

    projection = history["config"] / "projections/production-mount-main/versions" / history["v1"]
    install_root = (
        history["config"] / "installs/claude-code/production-mount-main/versions" / history["v1"]
    )

    # The immutable projection must not gain the marketplace manifest.
    assert not (projection / "claude-code/.claude-plugin").exists()
    manifest = json.loads(
        (install_root / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
    )
    assert manifest["plugins"][0]["source"].startswith("./plugins/")
    assert manifest["description"]
    assert manifest["owner"]["name"] == "PAL"

    # The install root carries a byte-identical copy of the projected plugin.
    plugin_name = manifest["plugins"][0]["name"]
    assert tree_digest(install_root / "plugins" / plugin_name) == tree_digest(
        projection / "claude-code/plugins" / plugin_name
    )

    # Codex keeps its own marketplace inside the projection and gains no install root.
    assert (projection / "codex/marketplace/.agents/plugins/marketplace.json").is_file()
    assert not (history["config"] / "installs/codex").exists()

    # Rolling forward and back must still work with the unchanged projection.
    activate_production(history["library"], history["v2"], config_root=history["config"])
    activate_production(history["library"], history["v1"], config_root=history["config"])
    assert active_version(history) == history["v1"]


def test_acc_011_historical_mount_validation_does_not_require_new_install_layout(
    tmp_path: Path,
    simulated_clis: SimulatedCodex,
) -> None:
    """A pre-persistent-install mount remains readable without creating new state."""

    history = production_history(tmp_path)
    activated = activate_production(
        history["library"], history["v1"], config_root=history["config"]
    )
    bundle = Path(activated["bundle"])
    projection = history["config"] / f"projections/production-mount-main/versions/{history['v1']}"
    before = (tree_digest(bundle.parent), tree_digest(projection))
    shutil.rmtree(history["config"] / "installs")

    validated = validate_production_mount(
        history["library"], history["v1"], config_root=history["config"]
    )

    assert validated["bundle_sha256"] == sha256_file(bundle)
    assert (tree_digest(bundle.parent), tree_digest(projection)) == before
    assert not (history["config"] / "installs").exists()


@pytest.mark.parametrize("cli_id", ["claude-code", "codex"])
def test_acc_008_reactivation_repairs_missing_install_without_rewriting_active(
    tmp_path: Path,
    simulated_clis: SimulatedCodex,
    cli_id: str,
) -> None:
    """Explicit reactivation can rebuild either target from the same immutable version."""

    history = production_history(tmp_path)
    activate_production(history["library"], history["v1"], config_root=history["config"])
    target = simulated_clis.claude if cli_id == "claude-code" else simulated_clis.codex
    previous_plugin_id = target.installed_plugin_id
    active_path = history["library"] / "production/active.json"
    active_before = active_path.read_bytes()
    target.installed_plugin_id = None
    if cli_id == "claude-code":
        shutil.rmtree(history["config"] / "installs")

    repaired = activate_production(history["library"], history["v1"], config_root=history["config"])

    assert repaired["idempotent"] is True
    assert target.installed_plugin_id == previous_plugin_id
    assert active_path.read_bytes() == active_before
    assert not transition_path(history).exists()


def test_publish_unit_serializes_the_whole_release_compose_activate_sequence(
    tmp_path: Path,
    simulated_clis: SimulatedCodex,
) -> None:
    """A competing publication must not split reading active from activating it.

    Without one lock over the whole sequence, the loser would compose from the
    stale active pointer and silently drop the concurrently published unit.
    """

    library, config = setup_library(tmp_path, "guided-publish-concurrency")
    commit_unit(tmp_path, library, config, "serialized-skill")

    lock_path = library / ".pal/locks/publication.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(PALError, match="locked by another operation"):
            publish_unit(library, "serialized-skill", config_root=config, sync=True)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    assert active_version({"library": library}) is None

    published = publish_unit(library, "serialized-skill", config_root=config, sync=True)
    assert active_version({"library": library}) == published["production_version_id"]


def test_publish_unit_activation_failure_preserves_previous_active_version(
    tmp_path: Path,
    simulated_clis: SimulatedCodex,
) -> None:
    library, config = setup_library(tmp_path, "guided-publish-failure")
    first_revision = commit_unit(tmp_path, library, config, "stable-skill")
    first = publish_unit(library, "stable-skill", config_root=config, sync=True)
    assert first["revision_id"] == first_revision

    next_revision = commit_unit(tmp_path, library, config, "failing-skill")
    next_release = create_release(library, "failing-skill", next_revision)
    target = compose_production(
        library,
        [first["release_id"], next_release["release_id"]],
    )
    simulated_clis.fail_install_version = target["production_version_id"]

    with pytest.raises(PALError, match="failed and was recovered"):
        publish_unit(library, "failing-skill", config_root=config, sync=True)

    assert active_version({"library": library}) == first["production_version_id"]
    assert not (config / "transitions/production/guided-publish-failure.json").exists()


def test_publish_cli_rejects_an_unknown_unit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    library, config = setup_library(tmp_path, "guided-publish-rejection")

    assert (
        cli_module.main(
            [
                "publish",
                "--library",
                str(library),
                "--unit",
                "missing-skill",
                "--config-root",
                str(config),
            ]
        )
        == 20
    )
    rejection = json.loads(capsys.readouterr().err)
    assert rejection["proof"] == "PAL_PUBLISH_REJECTED"
    assert "does not exist" in rejection["error"]


@pytest.mark.parametrize(
    ("cli_id", "expected_prefix", "passthrough"),
    [
        # Both targets discover the active plugin from their own persistent
        # installation, so neither command injects a session-scoped plugin root.
        ("claude-code", ["claude", "--model", "sonnet"], ["--model", "sonnet"]),
        ("codex", ["codex", "--model", "gpt-5"], ["--model", "gpt-5"]),
    ],
)
def test_launch_production_entry_starts_a_validated_interactive_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli_id: str,
    expected_prefix: list[str],
    passthrough: list[str],
) -> None:
    library = tmp_path / "library"
    config = tmp_path / "config"
    plugin_root = tmp_path / "production-plugin"
    library.mkdir()
    config.mkdir()
    plugin_root.mkdir()
    captured: dict[str, Any] = {}

    @contextmanager
    def runtime_context(*_args: Any, **_kwargs: Any) -> Any:
        yield {
            "library_root": library,
            "config_root": config,
            "executable": "claude" if cli_id == "claude-code" else "codex",
            "production_version_id": "production-v1",
            "plugin_root": plugin_root,
        }

    def launch(arguments: list[str], **kwargs: Any) -> int:
        captured["arguments"] = arguments
        captured.update(kwargs)
        return 17

    monkeypatch.setattr(production_mount_module, "active_runtime_context", runtime_context)
    monkeypatch.setattr(production_mount_module.subprocess, "call", launch)

    assert (
        launch_production_entry(
            library,
            cli_id,
            config_root=config,
            cli_arguments=passthrough,
        )
        == 17
    )
    assert captured["arguments"][: len(expected_prefix)] == expected_prefix
    assert captured["arguments"][-len(passthrough) :] == passthrough
    assert "--plugin-dir" not in captured["arguments"]
    assert str(plugin_root) not in captured["arguments"]
    assert "cwd" not in captured
    assert captured["env"]["PAL_LIBRARY_ROOT"] == str(library)
    assert captured["env"]["PAL_PRODUCTION_VERSION_ID"] == "production-v1"
    assert captured["env"]["PAL_CONFIG_ROOT"] == str(config)


def test_launch_use_cli_strips_passthrough_separator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = tmp_path / "library"
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        cli_module,
        "resolve_library_root",
        lambda value, **_kwargs: value or library,
    )

    def launch(*args: Any, **kwargs: Any) -> int:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(cli_module, "launch_production_entry", launch)

    assert (
        cli_module.main(
            [
                "launch",
                "use",
                "--library",
                str(library),
                "--cli",
                "claude-code",
                "--",
                "--model",
                "sonnet",
            ]
        )
        == 0
    )
    assert captured["args"] == (library, "claude-code")
    assert captured["kwargs"]["cli_arguments"] == ["--model", "sonnet"]


def test_production_mount_cli_envelopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    library = tmp_path / "library"
    result = {
        "library_id": "cli-envelope",
        "production_version_id": "production-v1",
    }
    monkeypatch.setattr(
        cli_module,
        "resolve_library_root",
        lambda value, **_kwargs: value or library,
    )
    monkeypatch.setattr(cli_module, "sync_production", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(
        cli_module,
        "publish_unit",
        lambda *_args, **_kwargs: {
            **result,
            "unit_id": "published-skill",
            "release_id": "release-v1",
        },
    )
    monkeypatch.setattr(
        cli_module,
        "recover_production",
        lambda *_args, **_kwargs: {
            "library_id": "cli-envelope",
            "recovered": False,
        },
    )
    monkeypatch.setattr(
        cli_module,
        "recover_skill_action",
        lambda *_args, **_kwargs: {"recovered": False},
    )
    monkeypatch.setattr(
        cli_module,
        "recover_deletion",
        lambda *_args, **_kwargs: {"deleted": False},
    )
    monkeypatch.setattr(
        cli_module,
        "recover_usage_transactions",
        lambda *_args, **_kwargs: {
            "recovered_usage_ids": [],
            "aborted_usage_ids": [],
            "pending_usage_ids": [],
        },
    )

    assert (
        cli_module.main(
            [
                "mount",
                "production",
                "--library",
                str(library),
                "--version",
                "production-v1",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["proof"] == "PAL_PRODUCTION_MOUNTED"

    assert (
        cli_module.main(
            [
                "production",
                "activate",
                "--library",
                str(library),
                "--version",
                "production-v1",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["proof"] == "PAL_PRODUCTION_ACTIVATED"

    with pytest.raises(SystemExit) as rejected:
        cli_module.main(
            ["production", "rollback", "--library", str(library), "--version", "production-v1"]
        )
    assert rejected.value.code == 2
    assert "invalid choice" in capsys.readouterr().err

    assert (
        cli_module.main(
            [
                "publish",
                "--library",
                str(library),
                "--unit",
                "published-skill",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["proof"] == "PAL_UNIT_PUBLISHED"

    assert cli_module.main(["recover", "--library", str(library)]) == 0
    recovery = json.loads(capsys.readouterr().out)
    assert recovery["skill_recovery"] == {"recovered": False}
    assert recovery["proof"] == "PAL_PRODUCTION_RECOVERED"
    assert recovery["deletion_recovery"] == {"deleted": False}


def test_legacy_trash_does_not_block_explicit_publication_or_sync(tmp_path, simulated_clis):
    from pal.publication import sync_production
    from tests.legacy_history import seed_trash

    history = production_history(tmp_path)
    library, config, old, current = (history[key] for key in ("library", "config", "v1", "v2"))
    activate_production(library, current, config_root=config)
    seed_trash(library, old, deleted=True, config_root=config)
    marker = config / "history/production-mount-main" / f"{old}.json"
    before = marker.read_bytes()
    apply_action(library, config, "delete", "second-mounted-skill")
    assert active_version(history) == current
    sync_production(library, config_root=config, expected_version_id=old)
    assert active_version(history) == old
    assert marker.read_bytes() == before


def test_skill_details_use_production_content_and_keep_directed_cli_variants(
    tmp_path: Path,
    simulated_clis: SimulatedCodex,
) -> None:
    from pal.skill_browser import skill_details
    from pal.web import _status

    library, config = setup_library(tmp_path, "skill-details")
    revision = commit_unit(tmp_path, library, config, "directed-details", directed=True)
    published = publish_unit(library, "directed-details", config_root=config, sync=True)
    details = skill_details(library, "directed-details")
    assert details["production"]["revision_id"] == revision
    assert details["production"]["artifacts"] == details["development"]["artifacts"]
    assert sorted(a["covered_clis"] for a in details["production"]["artifacts"]) == [
        ["claude-code"],
        ["codex"],
    ]
    # Development damage must never be presented as deployed content.
    development_skill(library, "directed-details", revision).write_text(
        "changed development", encoding="utf-8"
    )
    unit = _status(library, config)["units"][0]
    assert unit["description_source"] == "production"
    assert unit["description"] == "Exercise the immutable publishing workflow."
    with pytest.raises(PALError):
        skill_details(library, "directed-details")
    assert active_version({"library": library}) == published["production_version_id"]


def test_skill_file_production_binding_rejects_changed_active(
    tmp_path: Path, simulated_clis: SimulatedCodex
) -> None:
    from pal.skill_browser import skill_details, skill_file

    history = production_history(tmp_path)
    library, config = history["library"], history["config"]
    activate_production(library, history["v1"], config_root=config)
    source = skill_details(library, "first-mounted-skill")["production"]
    artifact = source["artifacts"][0]
    query = {
        "unit_id": "first-mounted-skill",
        "source": "production",
        "revision_id": source["revision_id"],
        "version_id": source["version_id"],
        "artifact_id": artifact["artifact_id"],
        "path": artifact["canonical_path"],
    }
    assert skill_file(library, query)["kind"] == "markdown"
    activate_production(library, history["v2"], config_root=config)
    with pytest.raises(PALError, match="版本已变化"):
        skill_file(library, query)
    query["version_id"] = history["v2"]
    assert "first-mounted-skill" in skill_file(library, query)["content"]


def test_program_upgrade_preserves_old_production_projection_bytes(
    tmp_path, simulated_clis, monkeypatch
):
    """ACC-008/011/012: upgrading PAL preserves old projections and supports new publication."""
    import pal.publishing as publishing_module

    monkeypatch.setattr(production_mount_module, "__version__", "0.1.0")
    monkeypatch.setattr(publishing_module, "__version__", "0.1.0")
    library, config = setup_library(tmp_path, "upgrade-production")
    commit_unit(tmp_path, library, config, "first")
    old = publish_unit(library, "first", config_root=config, sync=True)
    version = old["production_version_id"]
    roots = [
        library / f"production/versions/{version}",
        config / f"projections/upgrade-production/versions/{version}",
        config / f"mounts/production/upgrade-production/versions/{version}",
    ]
    before = [tree_digest(root) for root in roots]
    monkeypatch.setattr(production_mount_module, "__version__", "0.2.0")
    monkeypatch.setattr(publishing_module, "__version__", "0.2.0")
    validate_production_mount(library, version, config_root=config)
    assert activate_production(library, version, config_root=config)["idempotent"]
    commit_unit(tmp_path, library, config, "second")
    published = publish_unit(library, "second", config_root=config, sync=True)
    validate_production_mount(library, published["production_version_id"], config_root=config)
    assert [tree_digest(root) for root in roots] == before
