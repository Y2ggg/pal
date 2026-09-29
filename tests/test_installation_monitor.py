"""MON-001..006: inventory truth, independent repair and fail-closed errors."""

import shutil
import subprocess

import pytest

from pal import adapters
from pal import installation_monitor as monitor
from pal import production_mount as mounts
from pal.errors import PALError
from pal.io import sha256_file
from pal.publication import publish_unit
from tests.test_adapters import setup_context
from tests.test_production_mount import compatible_targets, production_history
from tests.test_production_mount import simulated_clis as simulated_clis


@pytest.fixture
def creation_installations(tmp_path, monkeypatch):
    root, config = setup_context(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    result = {}
    for cli in ("claude-code", "codex"):
        adapter = adapters.prepare_creation_adapter(root, cli, config_root=config)
        version = f"{adapters.__version__}+{adapters.CREATION_ADAPTER_SUFFIX}"
        cache = (
            monitor._cache_root(adapter["marketplace_name"], adapters.PLUGIN_NAME, version)
            if cli == "codex"
            else tmp_path / "claude-cache"
        )
        shutil.copytree(adapter["plugin_root"], cache)
        entry = {"version": version, "enabled": True}
        entry.update(
            {
                "pluginId": f"{adapters.PLUGIN_NAME}@{adapter['marketplace_name']}",
                "installed": True,
                "source": {"path": adapter["plugin_root"]},
            }
            if cli == "codex"
            else {
                "id": f"{adapters.PLUGIN_NAME}@{adapter['marketplace_name']}",
                "installPath": str(cache),
            }
        )
        market = {
            "name": adapter["marketplace_name"],
            "root" if cli == "codex" else "path": adapter["marketplace_root"],
        }
        result[cli] = (entry, market, cache)
    return root, config, result


@pytest.mark.parametrize("cli", ["claude-code", "codex"])
def test_creation_reads_real_version_enabled_and_full_inventory(creation_installations, cli):
    _, config, installed = creation_installations
    entry, market, cache = installed[cli]

    def check():
        return monitor._creation_status(cli, config, [entry], [market])

    assert check()["state"] == "healthy"
    entry["version"] = "0.1.0+native.5"
    assert check()["state"] == "outdated"
    entry["version"] = f"{adapters.__version__}+{adapters.CREATION_ADAPTER_SUFFIX}"
    entry["enabled"] = False
    assert check()["state"] == "disabled"
    entry["enabled"] = True
    (cache / "unexpected.md").write_text("drift")
    assert check()["state"] == "drift"
    assert monitor._creation_status(cli, config, [], [market])["state"] == "missing"
    market["root" if cli == "codex" else "path"] = "/tmp/foreign"
    assert check()["state"] == "conflict"


@pytest.mark.parametrize("cli", ["claude-code", "codex"])
def test_bad_inventory_and_timeouts_are_unknown_per_target(
    creation_installations, monkeypatch, cli
):
    _, config, _ = creation_installations
    monkeypatch.setattr(monitor.shutil, "which", lambda _: "cli")
    monkeypatch.setattr(
        monitor,
        "_inventory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("cli", 8)),
    )
    result = monitor._inspect_target(cli, config, None, None)
    assert result["creation"]["state"] == result["mount"]["state"] == "unknown"
    assert not result["creation"]["repairable"]


def test_inventory_timeout_is_bounded_and_json_shape_is_checked(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, 0, "[]", "")

    monkeypatch.setattr(monitor.subprocess, "run", run)
    with pytest.raises(PALError, match="响应无效"):
        monitor._inventory("codex", "codex")
    assert calls[0]["timeout"] == 8


@pytest.fixture
def mounted(tmp_path, simulated_clis, monkeypatch):
    history = production_history(tmp_path)
    root, config = history["library"], history["config"]
    mounts.activate_production(root, history["v1"], config_root=config)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    _, plan = monitor._read_plan(root, config, "production-mount-main")
    inventories = {}
    for cli in ("claude-code", "codex"):
        source = mounts._plugin_root(plan, cli)
        version = mounts._semver_with_cachebuster(
            plan.version_id, plan.validated["production"]["pal_version"]
        )
        cache = (
            monitor._cache_root(plan.marketplace_name, plan.plugin_name, version)
            if cli == "codex"
            else tmp_path / "claude-installed"
        )
        shutil.copytree(source, cache)
        entry = {"version": version, "enabled": True}
        entry.update(
            {
                "pluginId": mounts._plugin_id(plan),
                "name": plan.plugin_name,
                "installed": True,
                "source": {"path": str(source)},
            }
            if cli == "codex"
            else {"id": mounts._claude_plugin_id(plan), "installPath": str(cache)}
        )
        market = {
            "name": plan.marketplace_name,
            "root" if cli == "codex" else "path": str(
                mounts._marketplace_root(plan) if cli == "codex" else plan.install_roots[cli]
            ),
        }
        inventories[cli] = (entry, market, cache)
    return history, plan, inventories


@pytest.mark.parametrize("cli", ["claude-code", "codex"])
def test_mount_missing_disabled_drift_and_foreign_source(mounted, cli):
    _, plan, inventories = mounted
    entry, market, cache = inventories[cli]

    def check():
        return monitor._mount_status(cli, plan, [entry], [market])

    assert check()["state"] == "healthy"
    assert monitor._mount_status(cli, plan, [], [market])["state"] == "missing"
    entry["enabled"] = False
    assert check()["state"] == "disabled"
    entry["enabled"] = True
    skill = next(cache.rglob("SKILL.md"))
    skill.write_text("changed")
    assert check()["state"] == "drift"
    market["root" if cli == "codex" else "path"] = "/tmp/foreign"
    assert check()["state"] == "conflict"


def test_python_runtime_cache_is_not_delivery_drift(tmp_path):
    source, cache = tmp_path / "source", tmp_path / "installed"
    source.mkdir()
    (source / "export.py").write_text("print('ok')")
    shutil.copytree(source, cache)
    bytecode = cache / "__pycache__"
    bytecode.mkdir()
    (bytecode / "export.cpython-314.pyc").write_bytes(b"runtime")
    mounts._verify_installed_tree(cache, source)
    (bytecode / "injected.cpython-314.pyc").write_bytes(b"unexpected")
    with pytest.raises(PALError, match="未交付文件"):
        mounts._verify_installed_tree(cache, source)
    (bytecode / "injected.cpython-314.pyc").unlink()
    (cache / "export.py").write_text("changed")
    with pytest.raises(PALError, match="不一致"):
        mounts._verify_installed_tree(cache, source)


def test_mount_repair_never_publishes_or_syncs_pending_content(
    mounted, simulated_clis, monkeypatch
):
    history, _, _ = mounted
    root, config = history["library"], history["config"]
    publish_unit(root, "second-mounted-skill", config_root=config)
    before = {name: sha256_file(root / f"production/{name}.json") for name in ("current", "active")}
    simulated_clis.codex.installed_plugin_id = None
    monkeypatch.setattr(adapters, "require_compatible_cli", lambda cli: compatible_targets()[cli])
    monkeypatch.setattr(monitor, "_inventory", lambda *_args, **_kwargs: [])
    monitor._repair_mount(root, config, "codex", history["v1"])
    assert simulated_clis.installed_plugin_id is not None
    assert before == {name: sha256_file(root / f"production/{name}.json") for name in before}
    with pytest.raises(PALError, match="已变化"):
        monitor._repair_mount(root, config, "codex", history["v2"])
    transition = mounts._transition_path(config, "production-mount-main")
    transition.parent.mkdir(exist_ok=True, parents=True)
    transition.write_text("{}")
    with pytest.raises(PALError, match="未完成"):
        monitor._repair_mount(root, config, "codex", history["v1"])


def test_monitor_never_reports_pointer_equality_as_healthy(mounted, monkeypatch):
    history, _, inventories = mounted
    monkeypatch.setattr(monitor.shutil, "which", lambda name: name)

    def inventory(cli, _executable, *, marketplaces=False):
        entry, market, _ = inventories[cli]
        return [market] if marketplaces else [] if cli == "codex" else [entry]

    monkeypatch.setattr(monitor, "_inventory", inventory)
    result = monitor.inspect_installations(history["library"], config_root=history["config"])
    states = {target["cli_id"]: target["mount"]["state"] for target in result["targets"]}
    assert states == {"claude-code": "healthy", "codex": "missing"}
    assert result["production_version_id"] == result["mounted_version_id"]
    assert result["checked_at"]


def test_creation_repair_is_separate_and_checks_required_version(
    creation_installations, monkeypatch
):
    root, config, _ = creation_installations
    called = []
    monkeypatch.setattr(
        adapters, "install_creation_adapter", lambda *args, **kwargs: called.append((args, kwargs))
    )
    monkeypatch.setattr(
        monitor,
        "inspect_installations",
        lambda *args, **kwargs: {
            "targets": [{"cli_id": "codex", "creation": {"state": "healthy"}}]
        },
    )
    before = sha256_file(root / "production/active.json")
    monitor.repair_installation(
        root,
        config_root=config,
        cli_id="codex",
        kind="creation",
        expected_version=f"{adapters.__version__}+{adapters.CREATION_ADAPTER_SUFFIX}",
    )
    assert called[0][1]["repair"] is True
    assert sha256_file(root / "production/active.json") == before
    with pytest.raises(PALError, match="版本已变化"):
        monitor.repair_installation(
            root, config_root=config, cli_id="codex", kind="creation", expected_version="old"
        )


@pytest.mark.parametrize("cli", ["claude-code", "codex"])
def test_pal_package_upgrade_recognizes_its_own_previous_marketplace(
    creation_installations, monkeypatch, cli
):
    _, config, installations = creation_installations
    entry, market, _ = installations[cli]
    monkeypatch.setattr(adapters, "__version__", "0.3.0")
    monkeypatch.setattr(monitor, "__version__", "0.3.0")
    state = monitor._creation_status(cli, config, [entry], [market])
    assert state["state"] == "outdated"
    assert state["expected_version"] == "0.3.0+native.8"
    assert state["installed_marketplace"] == market["name"]
