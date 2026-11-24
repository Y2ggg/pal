"""Portable library export/import and machine adoption tests.

Traceability: PRD-P0-002, PRD-P0-003, PRD-P0-005; ACC-001, ACC-012.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

import pal.production_mount as production_mount_module
from pal.errors import PALError, PathSafetyError, ProductionError
from pal.library import doctor_library, initialize_library
from pal.production_mount import activate_production, adopt_active_production
from pal.transfer import export_library, import_library
from tests.test_production_mount import SimulatedCodex, compatible_targets
from tests.test_publishing import (
    commit_unit,
    compose_production,
    create_release,
    setup_library,
)


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


def published_library(tmp_path: Path, library_id: str) -> tuple[Path, Path, str]:
    library_root, config_root = setup_library(tmp_path, library_id)
    revision_id = commit_unit(tmp_path, library_root, config_root, "transfer-skill")
    release = create_release(library_root, "transfer-skill", revision_id)
    production = compose_production(library_root, [release["release_id"]])
    version_id = production["production_version_id"]
    activate_production(library_root, version_id, config_root=config_root)
    return library_root, config_root, version_id


def test_export_import_rebinds_the_pointer_to_the_new_machine(
    tmp_path: Path, simulated_clis: SimulatedCodex
) -> None:
    library_root, _config_root, version_id = published_library(tmp_path, "transfer-main")
    archive = tmp_path / "carry" / "library.tar.gz"
    exported = export_library(library_root, archive)
    assert exported["active_production_version_id"] == version_id
    assert exported["file_count"] > 10
    assert archive.is_file()

    new_root = tmp_path / "second-machine" / "AgentAbilitiesStore"
    new_config = tmp_path / "second-machine" / "config"
    imported = import_library(archive, new_root, config_root=new_config)

    assert imported["adopted"] is True
    assert imported["active_pointer_rewritten"] is True
    assert imported["active_production_version_id"] == version_id
    assert imported["previous_backup"] is None
    active = json.loads((new_root / "production/active.json").read_text(encoding="utf-8"))
    assert active["active_production_version_id"] == version_id
    assert str(new_config) in active["production_mount_bundle"]["path"]
    doctor_library(new_root)

    pointer_before = (new_root / "production/active.json").read_bytes()
    repeated = adopt_active_production(new_root, config_root=new_config)
    assert repeated["active_pointer_rewritten"] is False
    assert (new_root / "production/active.json").read_bytes() == pointer_before


def test_adopt_refuses_a_present_but_drifting_bundle(
    tmp_path: Path, simulated_clis: SimulatedCodex
) -> None:
    library_root, config_root, version_id = published_library(tmp_path, "adopt-drift")
    bundle = config_root / "mounts/production/adopt-drift/versions" / version_id / "bundle.json"
    payload = json.loads(bundle.read_text(encoding="utf-8"))
    payload["pal_version"] = "9.9.9-tampered"
    bundle.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(PALError):
        adopt_active_production(library_root, config_root=config_root)
    assert json.loads(bundle.read_text(encoding="utf-8"))["pal_version"] == "9.9.9-tampered"


def test_import_refuses_diverged_replacement(
    tmp_path: Path, simulated_clis: SimulatedCodex
) -> None:
    library_root, _config_root, _version_id = published_library(tmp_path, "transfer-diverge")
    archive = tmp_path / "library.tar.gz"
    export_library(library_root, archive)
    target = tmp_path / "target" / "library"
    config = tmp_path / "target" / "config"
    import_library(archive, target, config_root=config)

    divergent = target / "releases/units/other/release-other/manifest.json"
    divergent.parent.mkdir(parents=True)
    divergent.write_text('{"unit_id": "other"}\n', encoding="utf-8")

    with pytest.raises(ProductionError, match="不可变内容"):
        import_library(archive, target, config_root=config, replace=True)
    assert divergent.is_file()


def test_import_refuses_an_existing_target_without_replace(
    tmp_path: Path, simulated_clis: SimulatedCodex
) -> None:
    library_root, _config_root, _version_id = published_library(tmp_path, "transfer-exists")
    archive = tmp_path / "library.tar.gz"
    export_library(library_root, archive)
    with pytest.raises(ProductionError, match="已存在"):
        import_library(archive, library_root, config_root=tmp_path / "config2")


def test_export_refuses_open_transactions_and_library_contained_output(
    tmp_path: Path, simulated_clis: SimulatedCodex
) -> None:
    library_root, _config_root, _version_id = published_library(tmp_path, "transfer-open")
    journal = library_root / ".pal/transactions/creation/open-case/transaction.json"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"state": "OPEN"}\n', encoding="utf-8")
    with pytest.raises(ProductionError, match="未完成的事务"):
        export_library(library_root, tmp_path / "blocked.tar.gz")
    journal.unlink()

    with pytest.raises(ProductionError, match="不能写进库目录"):
        export_library(library_root, library_root / "inside.tar.gz")


def test_import_rejects_path_escape_members(tmp_path: Path) -> None:
    archive = tmp_path / "escape.tar.gz"
    manifest = {
        "archive_format": "pal-library-archive/1",
        "library_id": "escape",
        "files": [],
    }
    with tarfile.open(archive, "w:gz") as handle:
        payload = json.dumps(manifest).encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(payload)
        handle.addfile(info, io.BytesIO(payload))
        escaped = b"x"
        bad = tarfile.TarInfo("library/../escape.txt")
        bad.size = len(escaped)
        handle.addfile(bad, io.BytesIO(escaped))

    with pytest.raises(PathSafetyError):
        import_library(archive, tmp_path / "target")


def test_import_rejects_a_tampered_file(tmp_path: Path, simulated_clis: SimulatedCodex) -> None:
    library_root, _config_root, _version_id = published_library(tmp_path, "transfer-tamper")
    archive = tmp_path / "library.tar.gz"
    export_library(library_root, archive)

    extracted = tmp_path / "unpacked"
    extracted.mkdir()
    with tarfile.open(archive, "r:gz") as handle:
        handle.extractall(extracted)
    victim = extracted / "library" / "library.json"
    victim.write_bytes(victim.read_bytes() + b"\n")
    with tarfile.open(archive, "w:gz") as handle:
        for path in sorted(extracted.rglob("*")):
            handle.add(path, arcname=path.relative_to(extracted), recursive=False)

    with pytest.raises(PALError):
        import_library(archive, tmp_path / "tampered-target")


def test_empty_library_round_trip_needs_no_adoption(tmp_path: Path) -> None:
    library_root = tmp_path / "empty"
    initialize_library(library_root, "empty-library")
    archive = tmp_path / "empty.tar.gz"
    export_library(library_root, archive)
    target = tmp_path / "empty-copy"
    imported = import_library(archive, target, config_root=tmp_path / "empty-config")
    assert imported["adopted"] is False
    assert imported["active_production_version_id"] is None
    doctor_library(target)
