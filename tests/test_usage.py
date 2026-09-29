"""Controlled P2 execution, records, and recovery tests.

Traceability: PRD-USE-001, PRD-USE-002, PRD-P3-001;
ACC-009, ACC-010, ACC-011, ACC-012.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

import pytest

import pal.cli as cli_module
import pal.production_mount as production_mount_module
import pal.usage as usage_module
from pal import locking as fcntl
from pal.compatibility import CliCompatibility
from pal.errors import PALError, UsageError
from pal.io import sha256_file
from pal.production_mount import activate_production
from pal.usage import (
    recover_usage_transactions,
    run_production_task,
    validate_usage_record,
)
from tests.test_production_mount import SimulatedCodex, compatible_targets, production_history


@pytest.fixture
def usage_clis(
    monkeypatch: pytest.MonkeyPatch,
) -> SimulatedCodex:
    simulated_clis = SimulatedCodex()
    monkeypatch.setattr(
        production_mount_module,
        "_detect_target_compatibilities",
        compatible_targets,
    )
    monkeypatch.setattr(
        production_mount_module,
        "_run_claude_validation",
        lambda _plan: None,
    )
    monkeypatch.setattr(
        production_mount_module,
        "_install_codex",
        simulated_clis.codex.install,
    )
    monkeypatch.setattr(
        production_mount_module,
        "_remove_codex",
        simulated_clis.codex.remove,
    )
    monkeypatch.setattr(
        production_mount_module,
        "_verify_codex_installed",
        simulated_clis.codex.verify,
    )
    monkeypatch.setattr(
        production_mount_module,
        "_install_claude",
        simulated_clis.claude.install,
    )
    monkeypatch.setattr(
        production_mount_module,
        "_remove_claude",
        simulated_clis.claude.remove,
    )
    monkeypatch.setattr(
        production_mount_module,
        "_verify_claude_installed",
        simulated_clis.claude.verify,
    )
    monkeypatch.setattr(
        production_mount_module,
        "_claude_installed_plugin_root",
        lambda plan, _installation: production_mount_module._plugin_root(plan, "claude-code"),
    )
    monkeypatch.setattr(
        production_mount_module,
        "require_compatible_cli",
        lambda cli_id: compatible_targets()[cli_id],
    )
    monkeypatch.setattr(
        production_mount_module,
        "_codex_cached_plugin_root",
        lambda plan, _installation: production_mount_module._plugin_root(plan, "codex"),
    )
    return simulated_clis


def activated_history(tmp_path: Path) -> dict[str, Any]:
    history = production_history(tmp_path)
    activate_production(history["library"], history["v1"], config_root=history["config"])
    return history


def runtime_events(
    context: dict[str, Any],
    *,
    outcome: str,
) -> tuple[list[dict[str, Any]], int | None, str | None]:
    candidate = context["skills"][0]
    if context["cli_id"] == "claude-code":
        session_id = "claude-session-controlled"
        events: list[dict[str, Any]] = [
            {
                "type": "system",
                "subtype": "init",
                "claude_code_version": context["cli_version"],
                "session_id": session_id,
                "plugins": [{"name": context["plugin_name"], "path": str(context["plugin_root"])}],
                "capabilities": ["interrupt_receipt_v1"],
                "skills": [
                    f"{context['plugin_name']}:{item['skill_name']}" for item in context["skills"]
                ],
            }
        ]
        if outcome != "unselected":
            events.extend(
                [
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Skill",
                                    "id": "tool-controlled",
                                    "input": {
                                        "skill": (
                                            f"{context['plugin_name']}:{candidate['skill_name']}"
                                        )
                                    },
                                }
                            ]
                        },
                    },
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tool-controlled",
                                    "is_error": False,
                                }
                            ]
                        },
                    },
                    {
                        "type": "user",
                        "isSynthetic": True,
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "Base directory for this skill: "
                                        f"{Path(candidate['runtime_skill_path']).parent}\n"
                                    ),
                                }
                            ]
                        },
                    },
                ]
            )
        if outcome != "interrupted":
            failed = outcome == "failed"
            events.append(
                {
                    "type": "result",
                    "subtype": "error_during_execution" if failed else "success",
                    "is_error": failed,
                    "session_id": session_id,
                    "result": "controlled failure" if failed else "controlled success",
                }
            )
    else:
        events = [{"type": "thread.started", "thread_id": "codex-thread-controlled"}]
        if outcome != "unselected":
            events.append(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": f"sed -n '1,220p' '{candidate['runtime_skill_path']}'",
                        "aggregated_output": f"---\nname: {candidate['skill_name']}\n---\n",
                        "status": "completed",
                        "exit_code": 0,
                    },
                }
            )
        events.append(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "controlled result"},
            }
        )
        if outcome != "interrupted":
            events.append({"type": "turn.failed" if outcome == "failed" else "turn.completed"})
    exit_code = None if outcome == "interrupted" else 7 if outcome == "failed" else 0
    interruption = "SIGINT" if outcome == "interrupted" else None
    return events, exit_code, interruption


def install_capture(monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    def capture(
        root: Path,
        transaction: dict[str, Any],
        context: dict[str, Any],
        _task: str,
    ) -> tuple[list[dict[str, Any]], int | None, str | None]:
        events, exit_code, interruption = runtime_events(context, outcome=outcome)
        with (root / "events.jsonl").open("ab", buffering=0) as event_file:
            persisted: list[dict[str, Any]] = []
            for event in events:
                line = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
                usage_module._persist_event(
                    event_file,
                    line,
                    root,
                    transaction,
                    context,
                    persisted,
                )
        usage_module._update_process(
            root,
            exited_at=usage_module.utc_now(),
            process_exit_code=exit_code,
        )
        return events, exit_code, interruption

    monkeypatch.setattr(usage_module, "_capture_cli", capture)


@pytest.mark.parametrize("cli_id", ["claude-code", "codex"])
def test_acc_009_010_successful_controlled_run_records_six_field_closure(
    tmp_path: Path,
    usage_clis: SimulatedCodex,
    monkeypatch: pytest.MonkeyPatch,
    cli_id: str,
) -> None:
    history = activated_history(tmp_path)
    install_capture(monkeypatch, "succeeded")

    result = run_production_task(
        history["library"],
        cli_id,
        "Use the active production Skill and return one controlled result.",
        config_root=history["config"],
    )

    assert result["status"] == "succeeded"
    record_path = Path(result["record"])
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["task"]["input"].startswith("Use the active")
    assert record["overall_result"]["status"] == "succeeded"
    assert record["cli"]["id"] == cli_id
    assert record["production_version_id"] == history["v1"]
    assert len(record["logical_units"]) == 1
    assert len(record["loaded_artifacts"]) == 1
    assert (
        validate_usage_record(history["library"], record_path, config_root=history["config"])[
            "usage_id"
        ]
        == result["usage_id"]
    )
    assert not any((history["library"] / ".pal/transactions/usage").iterdir())


def test_compatible_cli_upgrade_records_runtime_version_without_rewriting_mount(
    tmp_path: Path,
    usage_clis: SimulatedCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = activated_history(tmp_path)
    mount_record = history["config"] / (
        f"mounts/production/production-mount-main/versions/{history['v1']}/claude-code.json"
    )
    mounted_version = json.loads(mount_record.read_text(encoding="utf-8"))["cli_version"]
    updated = CliCompatibility(
        "claude-code",
        "claude",
        "2.1.237",
        "2.1.205",
        "probe-compatible-version",
        (),
    )
    monkeypatch.setattr(
        production_mount_module,
        "require_compatible_cli",
        lambda cli_id: updated if cli_id == "claude-code" else compatible_targets()[cli_id],
    )
    install_capture(monkeypatch, "succeeded")

    result = run_production_task(
        history["library"],
        "claude-code",
        "Use the active production Skill after a compatible CLI update.",
        config_root=history["config"],
    )

    record = json.loads(Path(result["record"]).read_text(encoding="utf-8"))
    assert mounted_version == "2.1.234"
    assert json.loads(mount_record.read_text(encoding="utf-8"))["cli_version"] == mounted_version
    assert result["cli_version"] == "2.1.237"
    assert result["compatibility_classification"] == "probe-compatible-version"
    assert record["cli"]["version"] == "2.1.237"


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_exit"),
    [("failed", "failed", 7), ("interrupted", "interrupted", None)],
)
def test_selected_failure_and_interruption_are_persisted(
    tmp_path: Path,
    usage_clis: SimulatedCodex,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected_status: str,
    expected_exit: int | None,
) -> None:
    history = activated_history(tmp_path)
    install_capture(monkeypatch, outcome)

    result = run_production_task(
        history["library"],
        "codex",
        f"Controlled {outcome} task",
        config_root=history["config"],
    )
    record = json.loads(Path(result["record"]).read_text(encoding="utf-8"))

    assert result["status"] == expected_status
    assert record["overall_result"]["process_exit_code"] == expected_exit
    if outcome == "interrupted":
        assert "PAL_RUN_INTERRUPTED" in record["overall_result"]["output"]
        assert record["evidence"]["completion"]["signal"] == "SIGINT"


def test_unselected_run_is_rejected_without_formal_record(
    tmp_path: Path,
    usage_clis: SimulatedCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = activated_history(tmp_path)
    install_capture(monkeypatch, "unselected")

    with pytest.raises(UsageError, match="did not prove"):
        run_production_task(
            history["library"],
            "codex",
            "Do not load any production Skill.",
            config_root=history["config"],
        )

    assert not list((history["library"] / "records/usage").glob("*/*/*.json"))
    assert not any((history["library"] / ".pal/transactions/usage").iterdir())


def test_other_installation_with_pal_path_prefix_never_creates_usage_record(
    tmp_path: Path,
    usage_clis: SimulatedCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRD-USE-002 / ACC-012: 来源错误不能被正常终局或恢复补成正式记录。"""
    history = activated_history(tmp_path)
    original_events = runtime_events

    def foreign_events(context: dict[str, Any], *, outcome: str):
        events, exit_code, interruption = original_events(context, outcome=outcome)
        original = Path(context["skills"][0]["runtime_skill_path"])
        foreign = original.with_name("SKILL.md.other-install")
        foreign.write_bytes(original.read_bytes())
        events[1]["item"]["command"] = f"cat {shlex.quote(str(foreign))}"
        events[1]["item"]["aggregated_output"] = foreign.read_text()
        return events, exit_code, interruption

    monkeypatch.setattr("tests.test_usage.runtime_events", foreign_events)
    install_capture(monkeypatch, "succeeded")
    with pytest.raises(UsageError, match="did not prove"):
        run_production_task(
            history["library"],
            "codex",
            "Use a separately installed Skill.",
            config_root=history["config"],
        )
    assert not list((history["library"] / "records/usage").glob("*/*/*.json"))
    assert not any((history["library"] / ".pal/transactions/usage").iterdir())


class SimulatedUsageCrash(BaseException):
    """Represents wrapper loss outside normal exception cleanup."""


@pytest.mark.parametrize(
    ("outcome", "recovered_count", "aborted_count"),
    [("interrupted", 1, 0), ("unselected", 0, 1)],
)
def test_stale_transaction_recovery_records_only_proven_selection(
    tmp_path: Path,
    usage_clis: SimulatedCodex,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    recovered_count: int,
    aborted_count: int,
) -> None:
    history = activated_history(tmp_path)

    def crashing_capture(
        root: Path,
        transaction: dict[str, Any],
        context: dict[str, Any],
        _task: str,
    ) -> tuple[list[dict[str, Any]], int | None, str | None]:
        events, _, _ = runtime_events(context, outcome=outcome)
        with (root / "events.jsonl").open("ab", buffering=0) as event_file:
            persisted: list[dict[str, Any]] = []
            for event in events:
                usage_module._persist_event(
                    event_file,
                    json.dumps(event, separators=(",", ":")).encode(),
                    root,
                    transaction,
                    context,
                    persisted,
                )
        raise SimulatedUsageCrash

    monkeypatch.setattr(usage_module, "_capture_cli", crashing_capture)
    with pytest.raises(SimulatedUsageCrash):
        run_production_task(
            history["library"],
            "codex",
            "Crash after the evidence prefix is durable.",
            config_root=history["config"],
        )

    recovered = recover_usage_transactions(history["library"])

    assert len(recovered["recovered_usage_ids"]) == recovered_count
    assert len(recovered["aborted_usage_ids"]) == aborted_count
    records = list((history["library"] / "records/usage").glob("*/*/*.json"))
    assert len(records) == recovered_count
    if records:
        record = json.loads(records[0].read_text(encoding="utf-8"))
        assert record["overall_result"]["status"] == "interrupted"
        assert record["evidence"]["completion"]["signal"] == "wrapper-recovery"


def test_append_failure_preserves_selected_transaction_for_success_recovery(
    tmp_path: Path,
    usage_clis: SimulatedCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = activated_history(tmp_path)
    install_capture(monkeypatch, "succeeded")
    real_append = usage_module._append_record
    monkeypatch.setattr(
        usage_module,
        "_append_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected append failure")),
    )

    with pytest.raises(OSError, match="injected append failure"):
        run_production_task(
            history["library"],
            "codex",
            "Complete the CLI turn before the record append fails.",
            config_root=history["config"],
        )

    transactions = list((history["library"] / ".pal/transactions/usage").iterdir())
    assert len(transactions) == 1
    assert json.loads((transactions[0] / "transaction.json").read_text())["state"] == "SELECTED"

    monkeypatch.setattr(usage_module, "_append_record", real_append)
    recovered = recover_usage_transactions(history["library"])
    record_path = next((history["library"] / "records/usage").glob("*/*/*.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))

    assert len(recovered["recovered_usage_ids"]) == 1
    assert record["overall_result"]["status"] == "succeeded"
    assert record["evidence"]["completion"]["kind"] == "terminal-cli-event"


def test_p2_runtime_rejects_active_production_mount_and_projection_drift(
    tmp_path: Path,
    usage_clis: SimulatedCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = activated_history(tmp_path)
    monkeypatch.setattr(
        usage_module,
        "_capture_cli",
        lambda *_args, **_kwargs: pytest.fail("drifted runtime reached the CLI process"),
    )
    active = history["library"] / "production/active.json"
    production_skill = next(
        (history["library"] / f"production/versions/{history['v1']}").glob("**/SKILL.md")
    )
    mount_bundle = next(
        (history["config"] / "mounts/production").glob(f"**/{history['v1']}/bundle.json")
    )
    projection_skill = next(
        (history["config"] / "projections").glob(f"**/{history['v1']}/**/SKILL.md")
    )
    targets = (active, production_skill, mount_bundle, projection_skill)
    for target in targets:
        original = target.read_bytes()
        try:
            if target == active:
                value = json.loads(original)
                value["production_mount_bundle"]["sha256"] = "0" * 64
                target.write_text(json.dumps(value), encoding="utf-8")
            else:
                target.write_bytes(original + b"\nDRIFT\n")
            with pytest.raises(PALError):
                run_production_task(
                    history["library"],
                    "claude-code",
                    "A drifted closure must never start the target CLI.",
                    config_root=history["config"],
                )
        finally:
            target.write_bytes(original)

    assert not list((history["library"] / "records/usage").glob("*/*/*.json"))
    assert not any((history["library"] / ".pal/transactions/usage").iterdir())


def test_acc_011_historical_record_survives_development_change_and_active_switch(
    tmp_path: Path,
    usage_clis: SimulatedCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = activated_history(tmp_path)
    install_capture(monkeypatch, "succeeded")
    result = run_production_task(
        history["library"],
        "claude-code",
        "Record a stable historical production association.",
        config_root=history["config"],
    )
    record_path = Path(result["record"])
    before = sha256_file(record_path)
    development = next(
        (history["library"] / "development/units/first-mounted-skill").glob("**/SKILL.md")
    )
    development.write_text(
        development.read_text(encoding="utf-8") + "\nDevelopment-only change.\n",
        encoding="utf-8",
    )
    activate_production(history["library"], history["v2"], config_root=history["config"])

    validated = validate_usage_record(
        history["library"], record_path, config_root=history["config"]
    )

    assert validated["production_version_id"] == history["v1"]
    assert sha256_file(record_path) == before


def test_usage_record_is_append_only_and_rejects_p3_fields(
    tmp_path: Path,
    usage_clis: SimulatedCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = activated_history(tmp_path)
    install_capture(monkeypatch, "succeeded")
    result = run_production_task(
        history["library"],
        "codex",
        "Create one append-only usage record.",
        config_root=history["config"],
    )
    record_path = Path(result["record"])
    record = json.loads(record_path.read_text(encoding="utf-8"))
    before = sha256_file(record_path)

    alias_parent = tmp_path / "path-alias"
    alias_parent.symlink_to(tmp_path, target_is_directory=True)
    alias_library = alias_parent / "library"
    alias_record = alias_library / record_path.relative_to(history["library"])
    assert (
        validate_usage_record(
            alias_library,
            alias_record,
            config_root=history["config"],
        )["usage_id"]
        == result["usage_id"]
    )

    parent = record_path.parent
    orphan = parent / ".orphan.json.injected.tmp"
    orphan.write_text("orphan", encoding="utf-8")

    with pytest.raises(UsageError, match="append-only"):
        usage_module._append_record(history["library"], record)
    assert not orphan.exists()
    assert sha256_file(record_path) == before

    lock_descriptor = os.open(parent / ".append.lock", os.O_RDWR)
    fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(usage_module, "LOCK_TIMEOUT_SECONDS", 0.01)
    try:
        with pytest.raises(UsageError, match="timed out"):
            usage_module._append_record(history["library"], record)
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)

    record["rating"] = 5
    with pytest.raises(PALError):
        usage_module._validate_record_value(
            record,
            history["library"],
            config_root=history["config"],
            record_path=None,
        )


def test_cli_run_validate_and_recover_surfaces_are_wired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    library = tmp_path / "library"
    task = tmp_path / "task.txt"
    task.write_text("Run through the CLI surface.", encoding="utf-8")
    monkeypatch.setattr(
        cli_module,
        "resolve_library_root",
        lambda _value, **_kwargs: library,
    )
    monkeypatch.setattr(
        cli_module,
        "run_production_task",
        lambda *_args, **_kwargs: {
            "status": "succeeded",
            "usage_id": "usage-cli",
            "record": "/tmp/usage-cli.json",
        },
    )

    assert (
        cli_module.main(
            [
                "run",
                "--library",
                str(library),
                "--cli",
                "codex",
                "--task-file",
                str(task),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["proof"] == "PAL_PRODUCTION_USAGE_RECORDED"

    monkeypatch.setattr(
        cli_module,
        "validate_usage_record",
        lambda *_args, **_kwargs: {"usage_id": "usage-cli", "status": "succeeded"},
    )
    assert (
        cli_module.main(
            [
                "usage",
                "validate",
                "--library",
                str(library),
                "--record",
                "/tmp/usage-cli.json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["proof"] == "PAL_USAGE_RECORD_VALID"

    monkeypatch.setattr(
        cli_module, "recover_production", lambda *_args, **_kwargs: {"stable": True}
    )
    monkeypatch.setattr(
        cli_module, "recover_skill_action", lambda *_args, **_kwargs: {"recovered": False}
    )
    monkeypatch.setattr(
        cli_module, "recover_deletion", lambda *_args, **_kwargs: {"deleted": False}
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
    assert cli_module.main(["recover", "--library", str(library)]) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["proof"] == "PAL_PRODUCTION_RECOVERED"
    assert recovered["usage_recovery"]["pending_usage_ids"] == []
