"""Setup-only Quickstart behavior tests.

Traceability: PRD-P0-001 through PRD-P0-005, PRD-MOUNT-001,
PRD-MOUNT-002, PRD-CREATE-001; ACC-001, ACC-002, ACC-012; product
decision 051 and requirement 014.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import pal.cli as cli_module
import pal.quickstart as quickstart_module
from pal.errors import QuickstartCancelled, QuickstartError, UsageError
from pal.quickstart import (
    QuickstartAnswers,
    collect_quickstart_answers,
    load_quickstart_answers,
    run_quickstart,
)


def answers(tmp_path: Path) -> QuickstartAnswers:
    return QuickstartAnswers(
        library_id="personal-plugins",
        library_path=tmp_path / "library",
        config_root=tmp_path / "config",
    )


def answer_payload(tmp_path: Path) -> dict[str, object]:
    value = answers(tmp_path)
    return {
        "schema_version": 2,
        "library_id": value.library_id,
        "library_path": str(value.library_path),
        "config_root": str(value.config_root),
    }


def fake_compatibility(cli_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        diagnostic=lambda: {
            "cli_id": cli_id,
            "actual_version": "9.9.9",
            "classification": "probe-compatible-version",
        }
    )


def test_interactive_setup_asks_only_for_library_and_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []
    messages: list[str] = []
    responses = iter([str(tmp_path / "personal-plugins"), ""])
    monkeypatch.setattr(
        quickstart_module,
        "default_config_root",
        lambda: tmp_path / "default-config",
    )

    result = collect_quickstart_answers(
        input_fn=lambda prompt: prompts.append(prompt) or next(responses),
        output_fn=messages.append,
    )

    assert result == QuickstartAnswers(
        library_id="personal-plugins",
        library_path=tmp_path / "personal-plugins",
        config_root=tmp_path / "default-config",
    )
    assert len(prompts) == 2
    assert prompts[0].startswith("保存目录")
    assert prompts[1].startswith("以上信息正确，开始设置")
    assert not any("Skill 调用名称" in prompt or "用途" in prompt for prompt in prompts)
    assert "不会创建或发布 Skill，不会启动模型，也不会生成使用记录。" in messages
    assert "- 不会执行：创建 Skill、调用模型、发布或生成新生产版本" in messages


def test_interactive_confirmation_retries_invalid_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages: list[str] = []
    responses = iter([str(tmp_path / "library"), "maybe", ""])
    monkeypatch.setattr(quickstart_module, "default_config_root", lambda: tmp_path / "config")

    collect_quickstart_answers(
        input_fn=lambda _prompt: next(responses),
        output_fn=messages.append,
    )

    assert messages.count("输入无效：请输入 y（是）或 n（否）。") == 1


def test_interactive_cancellation_happens_before_setup_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter([str(tmp_path / "library"), "n"])
    monkeypatch.setattr(quickstart_module, "default_config_root", lambda: tmp_path / "config")

    with pytest.raises(QuickstartCancelled, match="尚未修改任何文件"):
        collect_quickstart_answers(
            input_fn=lambda _prompt: next(responses),
            output_fn=lambda _message: None,
        )


def test_interactive_answers_derive_hidden_id_for_non_ascii_directory_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter([str(tmp_path / "我的外挂库"), ""])
    monkeypatch.setattr(quickstart_module, "default_config_root", lambda: tmp_path / "config")

    result = collect_quickstart_answers(
        input_fn=lambda _prompt: next(responses),
        output_fn=lambda _message: None,
    )

    assert result.library_id.startswith("library-")
    assert len(result.library_id) == len("library-") + 12


def test_interactive_answers_convert_camel_case_directory_to_hidden_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter([str(tmp_path / "AgentAbilitiesStore"), ""])
    monkeypatch.setattr(quickstart_module, "default_config_root", lambda: tmp_path / "config")

    result = collect_quickstart_answers(
        input_fn=lambda _prompt: next(responses),
        output_fn=lambda _message: None,
    )

    assert result.library_id == "agent-abilities-store"


def test_automatic_library_id_reuses_existing_manifest_identity(tmp_path: Path) -> None:
    library = tmp_path / "AgentAbilitiesStore"
    library.mkdir()
    (library / "library.json").write_text(
        json.dumps({"library_id": "existing-library-id"}),
        encoding="utf-8",
    )

    assert quickstart_module._automatic_library_id(library) == "existing-library-id"


def test_answers_file_v2_is_strict_and_rejects_secret_fields(tmp_path: Path) -> None:
    path = tmp_path / "answers.json"
    payload = answer_payload(tmp_path)
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_quickstart_answers(path)

    assert loaded == answers(tmp_path)
    payload["api_key"] = "must-not-be-stored"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(UsageError, match="unknown fields.*api_key"):
        load_quickstart_answers(path)


def test_answers_file_rejects_v1_skill_creation_contract(tmp_path: Path) -> None:
    path = tmp_path / "answers.json"
    payload = {
        **answer_payload(tmp_path),
        "schema_version": 1,
        "initiating_cli": "codex",
        "component_type": "skill",
        "skill_name": "obsolete-bootstrap-skill",
        "skill_purpose": "This must not be created.",
        "release_and_activate": True,
        "validation_task": None,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(UsageError, match="schema_version 1.*removed Skill creation"):
        load_quickstart_answers(path)


def test_answers_reject_boolean_schema_version_and_overlapping_roots(tmp_path: Path) -> None:
    path = tmp_path / "answers.json"
    payload = answer_payload(tmp_path)
    payload["schema_version"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(UsageError, match="schema_version must be an integer"):
        load_quickstart_answers(path)

    payload = answer_payload(tmp_path)
    payload["config_root"] = str(Path(payload["library_path"]) / "config")
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(UsageError, match="must not overlap"):
        load_quickstart_answers(path)


def _patch_setup(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    value: QuickstartAnswers,
    *,
    active_version_id: str | None = None,
) -> None:
    monkeypatch.setattr(
        quickstart_module,
        "detect_cli_compatibility",
        lambda cli_id: events.append(f"compatibility:{cli_id}") or fake_compatibility(cli_id),
    )

    def initialize(path: Path, library_id: str) -> dict[str, object]:
        events.append("p0:init")
        path.mkdir(exist_ok=True)
        return {"library_id": library_id}

    monkeypatch.setattr(quickstart_module, "initialize_library", initialize)
    monkeypatch.setattr(
        quickstart_module,
        "check_library_contents",
        lambda _path: events.append("content-doctor") or {"content_checks": []},
    )
    monkeypatch.setattr(
        quickstart_module,
        "doctor_library",
        lambda _path: (
            events.append("doctor")
            or {
                "library_id": value.library_id,
                "active_production_version_id": active_version_id,
            }
        ),
    )
    monkeypatch.setattr(
        quickstart_module,
        "mount_config",
        lambda _library, cli_id, **_kwargs: events.append(f"mount:{cli_id}") or {"cli_id": cli_id},
    )
    monkeypatch.setattr(
        quickstart_module,
        "install_creation_adapter",
        lambda _library, cli_id, **_kwargs: (
            events.append(f"adapter:{cli_id}") or {"cli_id": cli_id}
        ),
    )
    monkeypatch.setattr(
        quickstart_module,
        "bind_default_creation_library",
        lambda _library, **_kwargs: events.append("binding") or {"idempotent": False},
    )


def test_new_library_setup_stops_before_skill_creation_and_production(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = answers(tmp_path)
    events: list[str] = []
    _patch_setup(monkeypatch, events, value)
    monkeypatch.setattr(
        quickstart_module,
        "validate_production_mount",
        lambda *_args, **_kwargs: pytest.fail("an empty library has nothing to validate"),
    )

    result = run_quickstart(value, output_fn=lambda _message: None)

    assert result["completed"] is True
    assert result["setup_status"] == "creation-ready"
    assert result["creation_ready"] is True
    assert result["production_ready"] is False
    assert result["active_production_version_id"] is None
    assert result["plugin_name"] is None
    assert "$pal-create-skill" in result["next_action"]
    assert events == [
        "compatibility:claude-code",
        "compatibility:codex",
        "p0:init",
        "doctor",
        "mount:claude-code",
        "mount:codex",
        "adapter:claude-code",
        "adapter:codex",
        "binding",
        "content-doctor",
    ]


def test_existing_empty_library_directory_is_initialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = answers(tmp_path)
    value.library_path.mkdir()
    events: list[str] = []
    _patch_setup(monkeypatch, events, value)

    result = run_quickstart(value, output_fn=lambda _message: None)

    assert result["library_reused"] is False
    assert "p0:init" in events


def test_existing_mounted_production_is_revalidated_without_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = answers(tmp_path)
    value.library_path.mkdir()
    (value.library_path / "library.json").write_text("{}", encoding="utf-8")
    events: list[str] = []
    _patch_setup(monkeypatch, events, value, active_version_id="production-active")
    monkeypatch.setattr(
        quickstart_module,
        "initialize_library",
        lambda *_args, **_kwargs: pytest.fail("an existing library must not be initialized"),
    )
    monkeypatch.setattr(
        quickstart_module,
        "validate_production_mount",
        lambda _library, version_id, **_kwargs: (
            events.append(f"validate-mount:{version_id}")
            or {"plugin_name": "pal-production-existing"}
        ),
    )
    monkeypatch.setattr(
        quickstart_module,
        "validate_production_version",
        lambda _library, version_id: {
            "production_version_id": version_id,
            "releases": [{"unit_id": "existing"}],
        },
    )

    result = run_quickstart(value, output_fn=lambda _message: None)

    assert result["library_reused"] is True
    assert result["setup_status"] == "production-ready"
    assert result["production_ready"] is True
    assert result["active_production_version_id"] == "production-active"
    assert result["plugin_name"] == "pal-production-existing"
    assert "直接启动 claude 或 codex" in result["next_action"]
    assert events == [
        "compatibility:claude-code",
        "compatibility:codex",
        "doctor",
        "mount:claude-code",
        "mount:codex",
        "adapter:claude-code",
        "adapter:codex",
        "binding",
        "validate-mount:production-active",
        "content-doctor",
    ]


def test_existing_empty_active_production_remains_creation_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = answers(tmp_path)
    value.library_path.mkdir()
    (value.library_path / "library.json").write_text("{}", encoding="utf-8")
    events: list[str] = []
    _patch_setup(monkeypatch, events, value, active_version_id="production-empty")
    monkeypatch.setattr(
        quickstart_module,
        "validate_production_mount",
        lambda _library, version_id, **_kwargs: (
            events.append(f"validate-mount:{version_id}") or {"plugin_name": "pal-production-empty"}
        ),
    )
    monkeypatch.setattr(
        quickstart_module,
        "validate_production_version",
        lambda _library, version_id: {"production_version_id": version_id, "releases": []},
    )

    result = run_quickstart(value, output_fn=lambda _message: None)

    assert result["setup_status"] == "creation-ready"
    assert result["production_ready"] is False
    assert result["active_production_version_id"] == "production-empty"
    assert "$pal-create-skill" in result["next_action"]


def test_failure_reports_stage_completed_work_and_resume_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = answers(tmp_path)
    monkeypatch.setattr(
        quickstart_module,
        "detect_cli_compatibility",
        lambda _cli_id: (_ for _ in ()).throw(UsageError("probe failed")),
    )

    with pytest.raises(QuickstartError) as raised:
        run_quickstart(value, output_fn=lambda _message: None)

    assert raised.value.stage == "compatibility"
    assert raised.value.completed_stages == ()
    assert "重新运行" in raised.value.next_action


def quickstart_result(
    value: QuickstartAnswers, *, production_ready: bool = False
) -> dict[str, object]:
    return {
        "completed": True,
        "setup_status": "production-ready" if production_ready else "creation-ready",
        "library_id": value.library_id,
        "library_root": str(value.library_path),
        "production_ready": production_ready,
        "active_production_version_id": "production-123" if production_ready else None,
        "compatibility": {
            "claude-code": {"actual_version": "2.1.234"},
            "codex": {"actual_version": "0.155.0"},
        },
        "next_action": "创建并发布第一个真实业务 Skill。",
    }


def test_cli_interactive_quickstart_emits_human_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    value = answers(tmp_path)
    monkeypatch.setattr(cli_module, "_enable_interactive_line_editing", lambda: None)
    monkeypatch.setattr(cli_module, "collect_quickstart_answers", lambda **_kwargs: value)
    monkeypatch.setattr(
        cli_module,
        "run_quickstart",
        lambda _answers, **_kwargs: quickstart_result(value, production_ready=True),
    )

    assert cli_module.main(["quickstart"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "",
        "PAL 设置完成",
        "状态：生产就绪",
        f"外挂库：{value.library_path}",
        "Claude Code：检查通过（2.1.234）",
        "Codex：检查通过（0.155.0）",
        "配置挂载：已就绪",
        "创建入口：已就绪",
        "完整性检查：通过",
        "生产版本：production-123",
        "下一步：创建并发布第一个真实业务 Skill。",
    ]
    assert "{" not in captured.out


def test_cli_quickstart_answers_preserves_machine_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    value = answers(tmp_path)
    answer_path = tmp_path / "answers.json"
    answer_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli_module, "load_quickstart_answers", lambda _path: value)
    monkeypatch.setattr(
        cli_module,
        "run_quickstart",
        lambda _answers, **_kwargs: quickstart_result(value),
    )

    assert cli_module.main(["quickstart", "--answers", str(answer_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["proof"] == "PAL_QUICKSTART_COMPLETED"
    assert result["setup_status"] == "creation-ready"
    assert result["library_id"] == "personal-plugins"


def test_cli_quickstart_cancellation_is_a_clean_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "_enable_interactive_line_editing",
        lambda: events.append("line-editing-enabled"),
    )

    def cancel(**kwargs: object) -> QuickstartAnswers:
        assert events == ["line-editing-enabled"]
        assert kwargs["input_fn"] is cli_module._interactive_input
        assert kwargs["output_fn"] is print
        raise QuickstartCancelled("已取消，尚未修改任何文件。")

    monkeypatch.setattr(cli_module, "collect_quickstart_answers", cancel)

    assert cli_module.main(["quickstart"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "\n已取消：已取消，尚未修改任何文件。\n"
    assert "{" not in captured.err


def test_cli_interactive_quickstart_failure_is_human_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    value = answers(tmp_path)
    monkeypatch.setattr(cli_module, "_enable_interactive_line_editing", lambda: None)
    monkeypatch.setattr(cli_module, "collect_quickstart_answers", lambda **_kwargs: value)
    monkeypatch.setattr(
        cli_module,
        "run_quickstart",
        lambda _answers, **_kwargs: (_ for _ in ()).throw(
            QuickstartError(
                "doctor failed",
                stage="doctor",
                completed_stages=("compatibility", "p0"),
                next_action="执行 pal doctor 后重试。",
            )
        ),
    )

    assert cli_module.main(["quickstart"]) == cli_module.REJECTION_EXIT_CODE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "",
        "PAL 设置未完成",
        "原因：doctor failed",
        "停止阶段：doctor",
        "已完成：compatibility, p0",
        "下一步：执行 pal doctor 后重试。",
    ]
    assert "{" not in captured.err


def test_cli_quickstart_answers_failure_preserves_machine_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    value = answers(tmp_path)
    answer_path = tmp_path / "answers.json"
    answer_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli_module, "load_quickstart_answers", lambda _path: value)
    monkeypatch.setattr(
        cli_module,
        "run_quickstart",
        lambda _answers, **_kwargs: (_ for _ in ()).throw(
            QuickstartError(
                "doctor failed",
                stage="doctor",
                completed_stages=("compatibility", "p0"),
                next_action="执行 pal doctor 后重试。",
            )
        ),
    )

    assert (
        cli_module.main(["quickstart", "--answers", str(answer_path)])
        == cli_module.REJECTION_EXIT_CODE
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    result = json.loads(captured.err)
    assert result == {
        "completed_stages": ["compatibility", "p0"],
        "error": "doctor failed",
        "next_action": "执行 pal doctor 后重试。",
        "proof": "PAL_QUICKSTART_REJECTED",
        "stage": "doctor",
    }
