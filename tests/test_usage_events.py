"""CLI JSONL evidence inspection tests for controlled P2 runs.

Traceability: PRD-USE-001, PRD-USE-002; ACC-009, ACC-010, ACC-012.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import pytest

from pal.errors import UsageError
from pal.io import sha256_file
from pal.usage_events import inspect_claude_events, inspect_codex_events


def event_context(tmp_path: Path, cli_id: str) -> dict[str, Any]:
    plugin_root = tmp_path / "projection/plugin"
    runtime_root = tmp_path / "runtime/plugin"
    relative = "skills/demo-skill/SKILL.md"
    material = (
        "---\n"
        "name: demo-skill\n"
        "description: Prove controlled production usage.\n"
        "---\n\n"
        "# Instructions\n"
    )
    for root in (plugin_root, runtime_root):
        path = root / relative
        path.parent.mkdir(parents=True)
        path.write_text(material, encoding="utf-8")
    projected = plugin_root / relative
    runtime = runtime_root / relative
    return {
        "cli_id": cli_id,
        "cli_version": "2.1.234" if cli_id == "claude-code" else "0.147.0",
        "plugin_name": "pal-production-test",
        "marketplace_name": ("pal-production-test-market" if cli_id == "codex" else None),
        "plugin_root": str(plugin_root),
        "runtime_plugin_root": str(runtime_root),
        "skills": [
            {
                "unit_id": "demo-skill",
                "release_id": "release-demo",
                "artifact_id": "artifact-demo",
                "profile_id": "skill-md-v1-basic",
                "covered_clis": ["claude-code", "codex"],
                "tree_sha256": "0" * 64,
                "relative_skill_path": relative,
                "skill_name": "demo-skill",
                "skill_sha256": sha256_file(projected),
                "projected_skill_path": str(projected),
                "runtime_skill_path": str(runtime),
            }
        ],
    }


def claude_events(context: dict[str, Any], *, failed: bool = False) -> list[dict[str, Any]]:
    candidate = context["skills"][0]
    session_id = "claude-session-1"
    return [
        {
            "type": "system",
            "subtype": "init",
            "claude_code_version": context["cli_version"],
            "session_id": session_id,
            "plugins": [{"name": context["plugin_name"], "path": context["plugin_root"]}],
            "capabilities": ["interrupt_receipt_v1", "future-unrelated-capability"],
            "skills": [f"{context['plugin_name']}:{candidate['skill_name']}"],
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Skill",
                        "id": "tool-1",
                        "input": {"skill": f"{context['plugin_name']}:{candidate['skill_name']}"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "tool-1", "is_error": False}]
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
                            f"{Path(candidate['runtime_skill_path']).parent}\n\n# Skill"
                        ),
                    }
                ]
            },
        },
        {
            "type": "result",
            "subtype": "error_during_execution" if failed else "success",
            "is_error": failed,
            "session_id": session_id,
            "result": "task failed" if failed else "task completed",
        },
    ]


def codex_events(context: dict[str, Any], *, failed: bool = False) -> list[dict[str, Any]]:
    candidate = context["skills"][0]
    events: list[dict[str, Any]] = [
        {"type": "thread.started", "thread_id": "codex-thread-1"},
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": f"sed -n '1,220p' '{candidate['runtime_skill_path']}'",
                "aggregated_output": "---\nname: demo-skill\n---\n",
                "status": "completed",
                "exit_code": 0,
            },
        },
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "task result"},
        },
    ]
    events.append({"type": "turn.failed" if failed else "turn.completed"})
    return events


def test_claude_success_proves_namespaced_skill_and_same_session(tmp_path: Path) -> None:
    context = event_context(tmp_path, "claude-code")
    inspected = inspect_claude_events(claude_events(context), context)

    assert inspected.session_id == "claude-session-1"
    assert inspected.terminal_status == "succeeded"
    assert inspected.output == "task completed"
    assert [item.candidate["artifact_id"] for item in inspected.selections] == ["artifact-demo"]


def test_claude_selected_then_failed_remains_recordable(tmp_path: Path) -> None:
    context = event_context(tmp_path, "claude-code")
    inspected = inspect_claude_events(claude_events(context, failed=True), context)

    assert inspected.selections
    assert inspected.terminal_status == "failed"
    assert inspected.result_code == "error_during_execution"


def test_claude_parser_tolerates_new_fields_order_and_unrelated_events(tmp_path: Path) -> None:
    context = event_context(tmp_path, "claude-code")
    events = claude_events(context)
    init = events[0]
    init["future_top_level"] = {"nested": True}
    init["plugins"][0]["future_plugin_field"] = "ignored"
    init["plugin_errors"] = [
        {
            "plugin": "unrelated-plugin",
            "type": "invalid_manifest",
            "message": "unrelated plugin failed",
            "future_error_field": "ignored",
        }
    ]
    events.insert(2, {"type": "future.unrelated", "payload": {"order": 1}})
    reordered = {key: init[key] for key in reversed(list(init))}
    events[0] = reordered

    inspected = inspect_claude_events(events, context)

    assert inspected.runtime_capabilities == (
        "future-unrelated-capability",
        "interrupt_receipt_v1",
    )
    assert inspected.selections


def test_claude_target_plugin_error_and_missing_capabilities_fail_closed(tmp_path: Path) -> None:
    context = event_context(tmp_path, "claude-code")
    events = claude_events(context)
    events[0]["plugin_errors"] = [
        {
            "plugin": context["plugin_name"],
            "type": "invalid_manifest",
            "message": "invalid manifest",
        }
    ]
    with pytest.raises(UsageError, match="error for the production plugin"):
        inspect_claude_events(events, context)

    events = claude_events(context)
    events[0].pop("capabilities")
    with pytest.raises(UsageError, match="capabilities"):
        inspect_claude_events(events, context)

    events = claude_events(context)
    events[0]["capabilities"] = {"interrupt_receipt_v1": True}
    with pytest.raises(UsageError, match="capabilities"):
        inspect_claude_events(events, context)

    events = claude_events(context)
    events[0]["plugin_errors"] = [
        {"plugin": "unrelated-plugin", "message": "missing documented type"}
    ]
    with pytest.raises(UsageError, match="plugin_errors"):
        inspect_claude_events(events, context)


def test_claude_bare_skill_name_is_not_stable_loading_evidence(tmp_path: Path) -> None:
    context = event_context(tmp_path, "claude-code")
    events = claude_events(context)
    events[1]["message"]["content"][0]["input"]["skill"] = "demo-skill"

    with pytest.raises(UsageError, match="not tied to one successful load"):
        inspect_claude_events(events, context)


def test_claude_ignores_synthetic_payload_from_unrelated_skill(tmp_path: Path) -> None:
    context = event_context(tmp_path, "claude-code")
    events = claude_events(context)
    unrelated = tmp_path / "unrelated-skill"
    unrelated.mkdir()
    events.insert(
        1,
        {
            "type": "user",
            "isSynthetic": True,
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": f"Base directory for this skill: {unrelated}\n",
                    }
                ]
            },
        },
    )

    inspected = inspect_claude_events(events, context)

    assert inspected.selections[0].candidate["artifact_id"] == "artifact-demo"


def test_claude_rejects_selection_after_terminal_result(tmp_path: Path) -> None:
    context = event_context(tmp_path, "claude-code")
    events = claude_events(context)
    events.insert(1, events.pop())

    with pytest.raises(UsageError, match="does not precede completion"):
        inspect_claude_events(events, context)


def test_codex_success_proves_runtime_cache_read_and_terminal_turn(tmp_path: Path) -> None:
    context = event_context(tmp_path, "codex")
    inspected = inspect_codex_events(codex_events(context), context)

    assert inspected.session_id == "codex-thread-1"
    assert inspected.terminal_status == "succeeded"
    assert inspected.output == "task result"
    assert inspected.selections[0].candidate["artifact_id"] == "artifact-demo"


def test_codex_selected_then_failed_remains_recordable(tmp_path: Path) -> None:
    context = event_context(tmp_path, "codex")
    inspected = inspect_codex_events(codex_events(context, failed=True), context)

    assert inspected.selections
    assert inspected.terminal_status == "failed"
    assert inspected.result_code == "turn-failed"


def test_codex_parser_tolerates_new_fields_and_unrelated_events(tmp_path: Path) -> None:
    context = event_context(tmp_path, "codex")
    events = codex_events(context)
    events[0]["future_thread_field"] = [1, 2, 3]
    events[1]["item"]["future_command_field"] = {"ignored": True}
    events.insert(2, {"type": "future.progress", "payload": "ignored"})

    inspected = inspect_codex_events(events, context)

    assert inspected.selections
    assert inspected.terminal_status == "succeeded"


def test_codex_source_projection_read_is_not_runtime_loading_evidence(tmp_path: Path) -> None:
    context = event_context(tmp_path, "codex")
    events = codex_events(context)
    events[1]["item"]["command"] = (
        f"sed -n '1,220p' '{context['skills'][0]['projected_skill_path']}'"
    )

    inspected = inspect_codex_events(events, context)

    assert inspected.selections == ()


def test_recovery_can_recheck_production_after_codex_cache_is_gone(tmp_path: Path) -> None:
    context = event_context(tmp_path, "codex")
    events = codex_events(context)
    Path(context["skills"][0]["runtime_skill_path"]).unlink()

    inspected = inspect_codex_events(events, context, verify_runtime=False)

    assert inspected.selections


@pytest.mark.parametrize("case", ["suffix-copy", "path-echo", "foreign-read-with-comment"])
def test_codex_does_not_attribute_other_sources_to_pal(tmp_path: Path, case: str) -> None:
    """PRD-USE-002 / ACC-012: 其他来源的同名同内容文件不能冒充 PAL 挂载。"""
    context = event_context(tmp_path, "codex")
    candidate = context["skills"][0]
    original = Path(candidate["runtime_skill_path"])
    foreign = original.with_name("SKILL.md.other-install")
    foreign.write_bytes(original.read_bytes())
    events = codex_events(context)
    commands = {
        "suffix-copy": f"cat {shlex.quote(str(foreign))}",
        "path-echo": f"printf '%s\\n' {shlex.quote(str(original))} 'name: demo-skill'",
        "foreign-read-with-comment": f"cat {shlex.quote(str(foreign))} # {original}",
    }
    events[1]["item"]["command"] = commands[case]
    events[1]["item"]["aggregated_output"] = (
        f"{original}\nname: demo-skill\n" if case == "path-echo" else foreign.read_text()
    )

    assert inspect_codex_events(events, context).selections == ()


@pytest.mark.parametrize("reader", ["cat", "cat --", "sed -n '1,220p'"])
@pytest.mark.parametrize("wrapper", [None, "/bin/zsh", "/bin/bash", "/bin/sh"])
def test_codex_exact_read_handles_quoted_paths_and_shell_wrappers(
    tmp_path: Path, reader: str, wrapper: str | None
) -> None:
    context = event_context(tmp_path / "space and apostrophe's (directory) [literal]*?", "codex")
    candidate = context["skills"][0]
    command = f"{reader} {shlex.quote(candidate['runtime_skill_path'])}"
    if wrapper:
        command = f"{wrapper} -lc {shlex.quote(command)}"
    events = codex_events(context)
    events[1]["item"]["command"] = command
    assert inspect_codex_events(events, context).selections[0].candidate == candidate


@pytest.mark.parametrize(
    "command",
    [
        "printf '%s' {path}",
        "cat /other/SKILL.md; echo {path}",
        "cat /other/SKILL.md > {path}",
        "echo $(cat {path})",
        "cat {path} /other/SKILL.md",
        "cat {path} '",
    ],
)
def test_codex_ambiguous_shell_text_cannot_prove_pal_origin(tmp_path: Path, command: str) -> None:
    context = event_context(tmp_path, "codex")
    events = codex_events(context)
    events[1]["item"]["command"] = command.format(
        path=shlex.quote(context["skills"][0]["runtime_skill_path"])
    )
    assert inspect_codex_events(events, context).selections == ()


@pytest.mark.parametrize("suffix", ["*", "?", "[ab]", "#other-install"])
def test_codex_unquoted_shell_metacharacters_cannot_prove_literal_source(
    tmp_path: Path, suffix: str
) -> None:
    """PRD-USE-002 / ACC-012: shell 展开或文件名中的 # 不能改变来源匹配。"""
    root = tmp_path if suffix.startswith("#") else tmp_path / f"binding{suffix}"
    context = event_context(root, "codex")
    events = codex_events(context)
    path = context["skills"][0]["runtime_skill_path"]
    if suffix.startswith("#"):
        foreign = Path(path + suffix)
        foreign.write_bytes(Path(path).read_bytes())
        path = str(foreign)
    events[1]["item"]["command"] = f"cat {path}"
    assert inspect_codex_events(events, context).selections == ()


def test_codex_identical_skill_in_another_binding_does_not_change_origin(tmp_path: Path) -> None:
    old = event_context(tmp_path / "old-library-version", "codex")
    new = event_context(tmp_path / "new-library-version", "codex")
    old["skills"][0].update(artifact_id="artifact-old", release_id="release-old")
    new["skills"][0].update(artifact_id="artifact-new", release_id="release-new")
    assert old["skills"][0]["skill_sha256"] == new["skills"][0]["skill_sha256"]
    events = codex_events(old)
    assert inspect_codex_events(events, new).selections == ()
    assert inspect_codex_events(events, old).selections[0].candidate == old["skills"][0]


def test_codex_recorded_count_then_read_preserves_the_same_source(tmp_path: Path) -> None:
    context = event_context(tmp_path, "codex")
    path = shlex.quote(context["skills"][0]["runtime_skill_path"])
    events = codex_events(context)
    command = f"wc -l {path} && sed -n '1,99999p' {path}"
    events[1]["item"]["command"] = f"/bin/zsh -lc {shlex.quote(command)}"
    assert inspect_codex_events(events, context).selections
    command = f"wc -l /different/SKILL.md && sed -n '1,99999p' {path}"
    events[1]["item"]["command"] = f"/bin/zsh -lc {shlex.quote(command)}"
    assert inspect_codex_events(events, context).selections == ()


def test_claude_other_plugin_with_same_skill_name_is_excluded(tmp_path: Path) -> None:
    context = event_context(tmp_path, "claude-code")
    foreign = event_context(tmp_path / "another-plugin", "claude-code")
    events = claude_events(context)
    events[1]["message"]["content"][0]["input"]["skill"] = "another-plugin:demo-skill"
    events[3]["message"]["content"][0]["text"] = (
        "Base directory for this skill: "
        f"{Path(foreign['skills'][0]['runtime_skill_path']).parent}\n"
    )
    assert inspect_claude_events(events, context).selections == ()
