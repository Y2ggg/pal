"""Audit 28 closure: PRD-P0-002, ACC-011/012, MON-002/003/005."""

import json
import shutil
import subprocess
from pathlib import Path
from urllib.error import HTTPError

import pytest

from pal import adapters, creation, web
from pal import installation_monitor as monitor
from pal.cli import main
from pal.errors import PALError
from pal.io import tree_digest
from pal.publication import publish_unit, sync_production
from pal.publishing import validate_production_version
from tests.test_installation_monitor import creation_installations as creation_installations
from tests.test_production_mount import simulated_clis as simulated_clis
from tests.test_skill_updates import initial, update
from tests.test_web import _request, _running_server


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("filename", ["SKILL.md", "asset.bin"])
def test_doctor_and_web_reject_development_drift(
    tmp_path, monkeypatch, capsys, published, filename
):
    root, config, _ = initial(tmp_path)
    if published:
        publish_unit(root, "example", config_root=config)
    base = creation.inspect_creation(root, "example", config_root=config)
    monkeypatch.setattr(web, "inspect_installations", lambda *_a, **_k: {"targets": []})
    server, thread, url = _running_server(root, config)
    try:
        production = tree_digest(root / "production")
        file = Path(base["artifacts"][0]["skill_root"]) / filename
        file.write_bytes(file.read_bytes() + b"\ncorrupted")
        assert main(["doctor", "--library", str(root)]) == 20
        error = json.loads(capsys.readouterr().err)
        assert error["proof"] == "PAL_LIBRARY_DOCTOR_REJECTED"
        assert "开发库" in error["error"] and filename in error["error"]
        with pytest.raises(HTTPError) as rejected:
            _request(url, "/api/doctor", method="POST", body={})
        assert rejected.value.code == 409
        body = json.loads(rejected.value.read())
        assert "开发库" in body["error"] and filename in body["error"]
        assert tree_digest(root / "production") == production
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()


@pytest.mark.parametrize("layer", ["production", "mounted"])
def test_doctor_validates_both_distinct_production_and_mount(
    tmp_path, simulated_clis, capsys, layer
):
    root, config, _ = initial(tmp_path)
    old = publish_unit(root, "example", config_root=config)
    sync_production(root, config_root=config)
    opened = update(tmp_path, root, config)
    skill = Path(opened["candidates"][0]["skill_path"])
    skill.write_text(skill.read_text() + "\nNew content.\n")
    creation.commit_creation(root, opened["creation_id"], config_root=config)
    new = publish_unit(root, "example", config_root=config)
    selected = new if layer == "production" else old
    validated = validate_production_version(root, selected["production_version_id"])
    payload = validated["releases"][0]["artifacts"][0]["payload_root"]
    file = next(payload.rglob("asset.bin"))
    file.write_bytes(b"corrupted")
    before = tree_digest(root)
    assert main(["doctor", "--library", str(root)]) == 20
    error = json.loads(capsys.readouterr().err)["error"]
    assert ("生产库" if layer == "production" else "CLI 挂载库") in error
    assert "asset.bin" in error
    assert tree_digest(root) == before


def test_doctor_reports_each_current_layer_without_writing_content(tmp_path, capsys):
    root, _, _ = initial(tmp_path)
    before = tree_digest(root)
    assert main(["doctor", "--library", str(root)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["proof"] == "PAL_LIBRARY_HEALTHY"
    assert result["content_checks"] == [
        {"layer": "development", "label": "开发库", "units": 1, "state": "healthy"},
        {"layer": "production", "label": "生产库", "units": 0, "state": "empty"},
        {"layer": "mounted", "label": "CLI 挂载库", "units": 0, "state": "empty"},
    ]
    assert tree_digest(root) == before


@pytest.mark.parametrize("cli", ["claude-code", "codex"])
@pytest.mark.parametrize("installed_state", ["enabled", "disabled", "missing", "outdated"])
def test_source_damage_is_not_offered_as_cache_repair(
    creation_installations, monkeypatch, cli, installed_state
):
    root, config, installed = creation_installations
    entry, market, _ = installed[cli]
    source = Path(market["root" if cli == "codex" else "path"])
    file = next(source.rglob("SKILL.md"))
    file.write_text(file.read_text() + "\nSource damage.\n")
    if installed_state == "disabled":
        entry["enabled"] = False
    elif installed_state == "outdated":
        entry["version"] = "0.0.1"
    entries = [] if installed_state == "missing" else [entry]
    report = monitor._creation_status(cli, config, entries, [market])
    assert report["state"] == "source-drift" and not report["repairable"]
    assert "同版本备份" in report["message"] and str(config) in report["message"]
    before = tree_digest(config)

    def forbidden(*_args, **_kwargs):
        pytest.fail("source damage must fail before any CLI invocation")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(PALError, match="源文件异常"):
        monitor.repair_installation(
            root,
            config_root=config,
            cli_id=cli,
            kind="creation",
            expected_version=f"{adapters.__version__}+{adapters.CREATION_ADAPTER_SUFFIX}",
        )
    assert tree_digest(config) == before


@pytest.mark.parametrize("cli", ["claude-code", "codex"])
def test_source_inventory_and_cache_damage_have_different_remedies(creation_installations, cli):
    _, config, installed = creation_installations
    entry, market, cache = installed[cli]
    (cache / "unexpected.md").write_text("cache damage")
    report = monitor._creation_status(cli, config, [entry], [market])
    assert report["state"] == "drift" and report["repairable"]
    source = (
        config / "targets/creation" / adapters.CREATION_LAYOUT_VERSION / adapters.__version__ / cli
    )
    (source / "unexpected.md").write_text("source damage outside plugin directory")
    report = monitor._creation_status(cli, config, [entry], [market])
    assert report["state"] == "source-drift" and not report["repairable"]


@pytest.mark.parametrize("cli", ["claude-code", "codex"])
def test_absent_source_can_be_installed_but_monitor_remains_read_only(creation_installations, cli):
    _, config, _ = creation_installations
    source = (
        config / "targets/creation" / adapters.CREATION_LAYOUT_VERSION / adapters.__version__ / cli
    )
    shutil.rmtree(source)
    before = tree_digest(config)
    report = monitor._creation_status(cli, config, [], [])
    assert report["state"] == "missing" and report["repairable"]
    assert tree_digest(config) == before
