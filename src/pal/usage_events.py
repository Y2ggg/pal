"""Version-bound Claude Code and Codex JSONL evidence inspection.

The inspectors accept incomplete prefixes so the usage transaction can become
``SELECTED`` as soon as an immutable production Skill load is proven.  A final
usage record still requires a terminal event, a failed process exit, or an PAL
interruption envelope.

Traceability: PRD-USE-001, PRD-USE-002; ACC-009, ACC-010, ACC-012.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import IntegrityError, UsageError
from .io import sha256_file
from .platform_support import is_link
from .targets import target_driver


@dataclass(frozen=True)
class ObservedSelection:
    candidate: dict[str, Any]
    event_index: int


@dataclass(frozen=True)
class EventInspection:
    session_id: str | None
    selections: tuple[ObservedSelection, ...]
    terminal_status: str | None
    output: str | None
    result_code: str | None
    completion_event_index: int | None
    runtime_capabilities: tuple[str, ...] = ()


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise UsageError(f"{label} must be a non-empty string")
    return value


def _safe_result_code(value: object, fallback: str) -> str:
    if isinstance(value, str):
        normalized = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-._")
        if normalized:
            return normalized
    return fallback


def _message_content(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [item for item in content if isinstance(item, dict)]


def _candidate_by_name(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["skill_name"]: item for item in context["skills"]}


def _candidate_from_skill_value(
    value: object,
    context: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    plugin_name = context["plugin_name"]
    prefix = f"{plugin_name}:"
    if not value.startswith(prefix):
        return None
    name = value.removeprefix(prefix)
    return _candidate_by_name(context).get(name)


def _verify_candidate_file(
    candidate: dict[str, Any],
    *,
    verify_runtime: bool,
) -> None:
    field = "runtime_skill_path" if verify_runtime else "projected_skill_path"
    path = Path(candidate[field])
    if is_link(path) or not path.is_file():
        raise IntegrityError(f"observed production Skill is unavailable: {path}")
    if sha256_file(path) != candidate["skill_sha256"]:
        raise IntegrityError(f"observed production Skill digest drifted: {path}")


def inspect_claude_events(
    events: list[dict[str, Any]],
    context: dict[str, Any],
    *,
    verify_runtime: bool = True,
) -> EventInspection:
    init_events = [
        (index, event)
        for index, event in enumerate(events)
        if event.get("type") == "system" and event.get("subtype") == "init"
    ]
    if len(init_events) > 1:
        raise UsageError("Claude event stream contains multiple init events")
    session_id: str | None = None
    runtime_capabilities: tuple[str, ...] = ()
    if init_events:
        _, init = init_events[0]
        runtime_capabilities = target_driver("claude-code").validate_runtime_initialization(
            init,
            context,
        )
        session_id = _nonempty_string(init.get("session_id"), "Claude session_id")
        exposed = init.get("skills")
        expected_skills = {
            f"{context['plugin_name']}:{candidate['skill_name']}" for candidate in context["skills"]
        }
        if not isinstance(exposed, list) or not expected_skills.issubset(set(exposed)):
            raise UsageError("Claude init did not expose every active production Skill")

    pending: dict[str, dict[str, Any]] = {}
    successful_tools: set[str] = set()
    selections: dict[str, ObservedSelection] = {}
    for index, event in enumerate(events):
        if event.get("type") == "assistant":
            for item in _message_content(event):
                if item.get("type") != "tool_use" or item.get("name") != "Skill":
                    continue
                inputs = item.get("input")
                candidate = (
                    _candidate_from_skill_value(inputs.get("skill"), context)
                    if isinstance(inputs, dict)
                    else None
                )
                if candidate is None:
                    continue
                tool_id = _nonempty_string(item.get("id"), "Claude Skill tool_use id")
                pending[tool_id] = candidate
        if event.get("type") == "user":
            for item in _message_content(event):
                if item.get("type") != "tool_result":
                    continue
                tool_id = item.get("tool_use_id")
                if isinstance(tool_id, str) and tool_id in pending and not item.get("is_error"):
                    successful_tools.add(tool_id)
        if event.get("type") != "user" or not event.get("isSynthetic"):
            continue
        for item in _message_content(event):
            text = item.get("text")
            if not isinstance(text, str):
                continue
            match = re.search(r"^Base directory for this skill: ([^\n]+)", text)
            if match is None:
                continue
            observed_base = Path(match.group(1)).resolve(strict=verify_runtime)
            candidate_bases = [
                candidate
                for candidate in context["skills"]
                if Path(candidate["runtime_skill_path"]).parent.resolve(strict=verify_runtime)
                == observed_base
            ]
            if not candidate_bases:
                continue
            matching = [
                (tool_id, candidate)
                for tool_id, candidate in pending.items()
                if tool_id in successful_tools
                and Path(candidate["runtime_skill_path"]).parent.resolve(strict=verify_runtime)
                == observed_base
            ]
            if len(matching) != 1:
                raise UsageError(
                    "Claude synthetic Skill payload is not tied to one successful load"
                )
            _, candidate = matching[0]
            _verify_candidate_file(candidate, verify_runtime=verify_runtime)
            selections.setdefault(
                candidate["artifact_id"],
                ObservedSelection(candidate=candidate, event_index=index),
            )

    terminal_events = [
        (index, event) for index, event in enumerate(events) if event.get("type") == "result"
    ]
    if len(terminal_events) > 1:
        raise UsageError("Claude event stream contains multiple terminal results")
    terminal_status = None
    output = None
    result_code = None
    completion_index = None
    if terminal_events:
        completion_index, terminal = terminal_events[0]
        terminal_session = _nonempty_string(
            terminal.get("session_id"), "Claude terminal session_id"
        )
        if session_id is None or terminal_session != session_id:
            raise UsageError("Claude selection and completion sessions differ")
        succeeded = terminal.get("subtype") == "success" and not terminal.get("is_error")
        terminal_status = "succeeded" if succeeded else "failed"
        result_code = _safe_result_code(
            terminal.get("subtype"),
            "success" if succeeded else "claude-result-failed",
        )
        value = terminal.get("result")
        if isinstance(value, str) and value:
            output = value
        elif not succeeded:
            output = json.dumps(
                {"proof": "PAL_CLI_TASK_FAILED", "result_code": result_code},
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            raise UsageError("Claude successful result has no readable output")
    if selections and session_id is None:
        raise UsageError("Claude production selection has no session identity")
    if selections:
        init_index = init_events[0][0]
        if any(selection.event_index <= init_index for selection in selections.values()):
            raise UsageError("Claude production selection precedes session initialization")
        if completion_index is not None and any(
            selection.event_index >= completion_index for selection in selections.values()
        ):
            raise UsageError("Claude production selection does not precede completion")
    return EventInspection(
        session_id=session_id,
        selections=tuple(selections.values()),
        terminal_status=terminal_status,
        output=output,
        result_code=result_code,
        completion_event_index=completion_index,
        runtime_capabilities=runtime_capabilities,
    )


def _codex_literal_read_path(command: str) -> str | None:
    """Identify one literal file operand without executing shell text.

    PRD-USE-002 / ACC-012: a printed path, comment or prefix collision is not
    evidence of reading the PAL installation. This deliberately accepts only
    the cat/sed forms observed in the version-bound POCs, optionally preceded
    by wc -l of the same file or wrapped in sh/bash/zsh -c/-lc. It does not
    determine the purpose of a genuine read.
    """
    for _ in range(3):
        if any(char in command for char in ("$", "`", "\n", "\r")):
            return None
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()*?[]{}~^")
            # Preserve # inside filenames; shlex comments otherwise truncate it.
            # Unquoted glob/expansion syntax splits operands and is rejected below.
            lexer.commenters = ""
            lexer.whitespace_split = True
            words = list(lexer)
        except ValueError:
            return None
        if not words:
            return None
        if words[0] in ("sh", "bash", "zsh", "/bin/sh", "/bin/bash", "/bin/zsh"):
            if len(words) != 3 or words[1] not in ("-c", "-lc"):
                return None
            command = words[2]
            continue
        counted_path = None
        if "&&" in words:
            # POC-02 also observed: wc -l FILE && sed -n '1,99999p' FILE.
            if (
                words.count("&&") != 1
                or words.index("&&") != 3
                or words[0] not in ("wc", "/bin/wc", "/usr/bin/wc")
                or words[1] != "-l"
                or not Path(words[2]).is_absolute()
            ):
                return None
            counted_path = str(Path(words[2]))
            words = words[4:]
        if not words or any(word and all(c in ";&|<>()" for c in word) for word in words):
            return None
        operands: list[str]
        if words[0] in ("cat", "/bin/cat", "/usr/bin/cat"):
            operands = words[1:]
        elif words[0] in ("sed", "/bin/sed", "/usr/bin/sed"):
            if len(words) < 4 or words[1] != "-n" or not re.fullmatch(r"1,[1-9][0-9]*p", words[2]):
                return None
            operands = words[3:]
        else:
            return None
        if operands[:1] == ["--"]:
            operands = operands[1:]
        if len(operands) != 1 or not Path(operands[0]).is_absolute():
            return None
        read_path = str(Path(operands[0]))
        return read_path if counted_path in (None, read_path) else None
    return None


def inspect_codex_events(
    events: list[dict[str, Any]],
    context: dict[str, Any],
    *,
    verify_runtime: bool = True,
) -> EventInspection:
    thread_events = [
        (index, event)
        for index, event in enumerate(events)
        if event.get("type") == "thread.started"
    ]
    if len(thread_events) > 1:
        raise UsageError("Codex event stream contains multiple thread.started events")
    session_id = (
        _nonempty_string(thread_events[0][1].get("thread_id"), "Codex thread_id")
        if thread_events
        else None
    )
    selections: dict[str, ObservedSelection] = {}
    messages: list[tuple[int, str]] = []
    for index, event in enumerate(events):
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text:
                messages.append((index, text))
            continue
        if item.get("type") != "command_execution":
            continue
        command = item.get("command")
        output = item.get("aggregated_output")
        if not isinstance(command, str) or not isinstance(output, str):
            continue
        read_path = _codex_literal_read_path(command)
        matching = [
            candidate
            for candidate in context["skills"]
            if read_path == str(Path(candidate["runtime_skill_path"]))
        ]
        if not matching:
            continue
        if len(matching) != 1:
            raise UsageError("Codex command references multiple production Skill paths")
        candidate = matching[0]
        if f"name: {candidate['skill_name']}" not in output:
            raise UsageError("Codex Skill read output does not prove the expected identity")
        if item.get("status") not in (None, "completed") or item.get("exit_code") not in (None, 0):
            raise UsageError("Codex production Skill read command did not complete successfully")
        _verify_candidate_file(candidate, verify_runtime=verify_runtime)
        selections.setdefault(
            candidate["artifact_id"],
            ObservedSelection(candidate=candidate, event_index=index),
        )

    completed = [
        (index, event)
        for index, event in enumerate(events)
        if event.get("type") == "turn.completed"
    ]
    failed = [
        (index, event) for index, event in enumerate(events) if event.get("type") == "turn.failed"
    ]
    if len(completed) > 1 or len(failed) > 1 or (completed and failed):
        raise UsageError("Codex event stream has an ambiguous terminal state")
    terminal_status = None
    output = None
    result_code = None
    completion_index = None
    if completed:
        completion_index = completed[0][0]
        terminal_status = "succeeded"
        result_code = "turn-completed"
        prior = [item for item in messages if item[0] < completion_index]
        if not prior:
            raise UsageError("Codex completed turn has no readable agent result")
        output = prior[-1][1]
    elif failed:
        completion_index = failed[0][0]
        terminal_status = "failed"
        result_code = "turn-failed"
        prior = [item for item in messages if item[0] < completion_index]
        output = (
            prior[-1][1]
            if prior
            else json.dumps(
                {"proof": "PAL_CLI_TASK_FAILED", "result_code": result_code},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    elif any(event.get("type") == "error" for event in events):
        completion_index = max(
            index for index, event in enumerate(events) if event.get("type") == "error"
        )
        terminal_status = "failed"
        result_code = "codex-error"
        output = json.dumps(
            {"proof": "PAL_CLI_TASK_FAILED", "result_code": result_code},
            sort_keys=True,
            separators=(",", ":"),
        )
    if selections and session_id is None:
        raise UsageError("Codex production selection has no thread identity")
    if selections:
        thread_index = thread_events[0][0]
        if any(selection.event_index <= thread_index for selection in selections.values()):
            raise UsageError("Codex production selection precedes thread initialization")
        if completion_index is not None and any(
            selection.event_index >= completion_index for selection in selections.values()
        ):
            raise UsageError("Codex production selection does not precede completion")
    return EventInspection(
        session_id=session_id,
        selections=tuple(selections.values()),
        terminal_status=terminal_status,
        output=output,
        result_code=result_code,
        completion_event_index=completion_index,
    )


def inspect_events(
    cli_id: str,
    events: list[dict[str, Any]],
    context: dict[str, Any],
    *,
    verify_runtime: bool = True,
) -> EventInspection:
    if cli_id == "claude-code":
        return inspect_claude_events(events, context, verify_runtime=verify_runtime)
    if cli_id == "codex":
        return inspect_codex_events(events, context, verify_runtime=verify_runtime)
    raise UsageError(f"unsupported usage event CLI: {cli_id}")


__all__ = ["EventInspection", "ObservedSelection", "inspect_events"]
