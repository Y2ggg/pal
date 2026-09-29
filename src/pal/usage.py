"""Controlled P2 execution and append-only usage association records.

Traceability: PRD-USE-001, PRD-USE-002, PRD-P3-001;
ACC-009, ACC-010, ACC-011, ACC-012.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from . import __version__
from . import locking as fcntl
from .components import SKILL_COMPONENT_TYPE_ID, component_type_driver
from .config_mount import resolve_config_root
from .errors import IntegrityError, PathSafetyError, UsageError
from .io import (
    atomic_replace_json,
    formatted_json_bytes,
    fsync_directory,
    sha256_bytes,
    sha256_file,
    write_new_bytes,
    write_new_json,
)
from .library import doctor_development_context, load_json_object, utc_now
from .maintenance import lifecycle_write
from .paths import (
    canonical_existing_root,
    normalize_relative_path,
    require_inside,
    require_safe_id,
)
from .platform_support import command_for_platform, is_link, process_exists
from .production_mount import (
    active_runtime_context,
    validate_production_mount,
)
from .publishing import validate_production_version
from .schema_catalog import TARGET_CLIS, validate_instance
from .usage_events import EventInspection

TRANSACTION_SCHEMA = "usage-transaction.schema.json"
RECORD_SCHEMA = "usage-record.schema.json"
LOCK_TIMEOUT_SECONDS = 5.0
TEMP_RECORD_PATTERN = re.compile(r"^\..+\.json\..+\.tmp$")
FORBIDDEN_P3_KEYS = {
    "attribution",
    "evaluation",
    "iteration",
    "rating",
    "recommendation",
    "score",
}


def _inspect_usage_events(
    cli_id: str,
    events: list[dict[str, Any]],
    context: dict[str, Any],
    *,
    verify_runtime: bool = True,
) -> EventInspection:
    inspection = component_type_driver(SKILL_COMPONENT_TYPE_ID).inspect_usage_events(
        cli_id,
        events,
        context,
        verify_runtime=verify_runtime,
    )
    if not isinstance(inspection, EventInspection):
        raise IntegrityError("artifact driver returned an invalid event inspection")
    return inspection


def _json_compatible_context(
    context: dict[str, Any], usage_id: str, task_id: str
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "usage_id": usage_id,
        "task_id": task_id,
        "library_id": context["library_id"],
        "library_root": str(context["library_root"]),
        "config_root": str(context["config_root"]),
        "cli_id": context["cli_id"],
        "cli_version": context["cli_version"],
        "production_version_id": context["production_version_id"],
        "production_manifest_sha256": context["production_manifest_sha256"],
        "active_sha256": context["active_sha256"],
        "mount_bundle_path": str(context["mount_bundle_path"]),
        "mount_bundle_sha256": context["mount_bundle_sha256"],
        "cli_mount_path": str(context["cli_mount_path"]),
        "cli_mount_sha256": context["cli_mount_sha256"],
        "projection_root": str(context["projection_root"]),
        "projection_tree_sha256": context["projection_tree_sha256"],
        "plugin_name": context["plugin_name"],
        "marketplace_name": context["marketplace_name"],
        "plugin_root": str(context["plugin_root"]),
        "runtime_plugin_root": str(context["runtime_plugin_root"]),
        "skills": context["skills"],
    }


def _absolute_context_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise UsageError(f"usage context {label} must be an absolute path")
    path = Path(value)
    if not path.is_absolute() or Path(os.path.abspath(path)) != path:
        raise UsageError(f"usage context {label} must be a normalized absolute path")
    return path


def _context_descendant(
    value: object,
    root: Path,
    label: str,
) -> Path:
    path = _absolute_context_path(value, label)
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise PathSafetyError(f"usage context {label} escapes its root: {path}") from exc
    return require_inside(root, relative, f"usage context {label}")


def _validate_context_shape(
    context: dict[str, Any],
    transaction_root: Path,
    *,
    require_runtime: bool,
) -> None:
    required = {
        "schema_version",
        "usage_id",
        "task_id",
        "library_id",
        "library_root",
        "config_root",
        "cli_id",
        "cli_version",
        "production_version_id",
        "production_manifest_sha256",
        "active_sha256",
        "mount_bundle_path",
        "mount_bundle_sha256",
        "cli_mount_path",
        "cli_mount_sha256",
        "projection_root",
        "projection_tree_sha256",
        "plugin_name",
        "marketplace_name",
        "plugin_root",
        "runtime_plugin_root",
        "skills",
    }
    if set(context) != required or context.get("schema_version") != 1:
        raise UsageError("usage context fields are invalid")
    require_safe_id(context.get("usage_id"), "usage context ID")
    require_safe_id(context.get("task_id"), "usage task ID")
    require_safe_id(context.get("library_id"), "usage library ID")
    require_safe_id(context.get("production_version_id"), "usage production version ID")
    cli_id = context.get("cli_id")
    cli_version = context.get("cli_version")
    if (
        cli_id not in TARGET_CLIS
        or not isinstance(cli_version, str)
        or re.fullmatch(r"\d+\.\d+\.\d+", cli_version) is None
    ):
        raise UsageError("usage context CLI identity is unsupported")
    if cli_id == "claude-code" and context.get("marketplace_name") is not None:
        raise UsageError("Claude usage context cannot contain a marketplace")
    if cli_id == "codex":
        require_safe_id(context.get("marketplace_name"), "usage marketplace ID")
    require_safe_id(context.get("plugin_name"), "usage plugin ID")
    for field in (
        "production_manifest_sha256",
        "active_sha256",
        "mount_bundle_sha256",
        "cli_mount_sha256",
        "projection_tree_sha256",
    ):
        value = context.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise UsageError(f"usage context {field} is invalid")
    if not isinstance(context.get("skills"), list) or not context["skills"]:
        raise UsageError("usage context has no production Skills")
    if transaction_root.name != context["usage_id"]:
        raise UsageError("usage context ID differs from its transaction directory")

    library_root = canonical_existing_root(
        _absolute_context_path(context.get("library_root"), "library_root")
    )
    if library_root != transaction_root.parents[3]:
        raise PathSafetyError("usage context library root differs from its transaction")
    config_root = canonical_existing_root(
        _absolute_context_path(context.get("config_root"), "config_root")
    )
    mount_bundle = _context_descendant(
        context.get("mount_bundle_path"), config_root, "mount_bundle_path"
    )
    cli_mount = _context_descendant(context.get("cli_mount_path"), config_root, "cli_mount_path")
    projection_root = _context_descendant(
        context.get("projection_root"), config_root, "projection_root"
    )
    plugin_root = _context_descendant(context.get("plugin_root"), config_root, "plugin_root")
    if not mount_bundle.is_file() or not cli_mount.is_file():
        raise UsageError("usage context production mount references must be files")
    if not projection_root.is_dir() or not plugin_root.is_dir():
        raise UsageError("usage context production projection references must be directories")
    if not plugin_root.is_relative_to(projection_root):
        raise PathSafetyError("usage context plugin root escapes its CLI projection")

    runtime_root = _absolute_context_path(context.get("runtime_plugin_root"), "runtime_plugin_root")
    if cli_id == "claude-code" and runtime_root != plugin_root:
        raise UsageError("Claude runtime plugin root must equal its source projection")
    if require_runtime:
        runtime_root = canonical_existing_root(runtime_root)

    candidate_fields = {
        "unit_id",
        "release_id",
        "artifact_id",
        "profile_id",
        "covered_clis",
        "tree_sha256",
        "relative_skill_path",
        "skill_name",
        "skill_sha256",
        "projected_skill_path",
        "runtime_skill_path",
    }
    seen_artifacts: set[str] = set()
    seen_names: set[str] = set()
    for candidate in context["skills"]:
        if not isinstance(candidate, dict) or set(candidate) != candidate_fields:
            raise UsageError("usage context Skill candidate fields are invalid")
        for field in ("unit_id", "release_id", "artifact_id", "profile_id", "skill_name"):
            require_safe_id(candidate.get(field), f"usage Skill {field}")
        covered = candidate.get("covered_clis")
        if (
            not isinstance(covered, list)
            or not covered
            or len(covered) != len(set(covered))
            or any(target not in TARGET_CLIS for target in covered)
            or cli_id not in covered
        ):
            raise UsageError("usage context Skill coverage is invalid")
        for field in ("tree_sha256", "skill_sha256"):
            value = candidate.get(field)
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise UsageError(f"usage context Skill {field} is invalid")
        relative = normalize_relative_path(
            candidate.get("relative_skill_path"),
            "usage context Skill path",
        )
        if relative != f"skills/{candidate['skill_name']}/SKILL.md":
            raise UsageError("usage context Skill name and path differ")
        projected = _absolute_context_path(
            candidate.get("projected_skill_path"),
            "projected_skill_path",
        )
        runtime = _absolute_context_path(
            candidate.get("runtime_skill_path"),
            "runtime_skill_path",
        )
        if projected != plugin_root / relative or runtime != runtime_root / relative:
            raise PathSafetyError("usage context Skill path escapes its plugin root")
        if is_link(projected) or not projected.is_file():
            raise IntegrityError(f"usage context projected Skill is unavailable: {projected}")
        if sha256_file(projected) != candidate["skill_sha256"]:
            raise IntegrityError(f"usage context projected Skill drifted: {projected}")
        if require_runtime:
            if is_link(runtime) or not runtime.is_file():
                raise IntegrityError(f"usage context runtime Skill is unavailable: {runtime}")
            if sha256_file(runtime) != candidate["skill_sha256"]:
                raise IntegrityError(f"usage context runtime Skill drifted: {runtime}")
        artifact_id = candidate["artifact_id"]
        skill_name = candidate["skill_name"]
        if artifact_id in seen_artifacts or skill_name in seen_names:
            raise UsageError("usage context contains duplicate Skill candidates")
        seen_artifacts.add(artifact_id)
        seen_names.add(skill_name)


def _mkdir_descendant(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PathSafetyError(f"usage path escapes its library root: {path}") from exc
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.exists() or is_link(cursor):
            if is_link(cursor) or not cursor.is_dir():
                raise PathSafetyError(f"usage path is not a real directory: {cursor}")
        else:
            cursor.mkdir()


def _safe_remove_transaction(path: Path, parent: Path) -> None:
    try:
        path.relative_to(parent)
    except ValueError as exc:  # pragma: no cover - defensive invariant
        raise PathSafetyError(
            f"refusing to clean usage transaction outside its root: {path}"
        ) from exc
    if is_link(path) or not path.name.startswith("usage-"):
        raise PathSafetyError(f"refusing to clean unexpected usage transaction: {path}")
    shutil.rmtree(path)
    fsync_directory(parent)


def _event_reference(path: Path) -> dict[str, str]:
    return {"path": path.name, "sha256": sha256_file(path)}


def _new_usage_id() -> str:
    return f"usage-{secrets.token_hex(16)}"


def _new_task_id() -> str:
    return f"task-{secrets.token_hex(16)}"


def _transaction_root(library_root: Path, usage_id: str) -> Path:
    require_safe_id(usage_id, "usage ID")
    parent = require_inside(
        library_root,
        ".pal/transactions/usage",
        "usage transaction parent",
    )
    return parent / usage_id


def _create_transaction(
    context: dict[str, Any],
    task: str,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    usage_id = _new_usage_id()
    task_id = _new_task_id()
    root = _transaction_root(context["library_root"], usage_id)
    root.mkdir()
    committed = False
    try:
        task_path = root / "task.txt"
        events_path = root / "events.jsonl"
        write_new_bytes(task_path, task.encode("utf-8"))
        write_new_bytes(events_path, b"")
        persisted_context = _json_compatible_context(context, usage_id, task_id)
        _validate_context_shape(persisted_context, root, require_runtime=True)
        write_new_json(root / "context.json", persisted_context)
        started_at = utc_now()
        process = {
            "schema_version": 1,
            "usage_id": usage_id,
            "hostname": socket.gethostname(),
            "wrapper_pid": os.getpid(),
            "child_pid": None,
            "started_at": started_at,
            "exited_at": None,
            "process_exit_code": None,
        }
        write_new_json(root / "process.json", process)
        transaction = {
            "schema_version": 1,
            "usage_id": usage_id,
            "library_id": context["library_id"],
            "cli_id": context["cli_id"],
            "state": "OPEN",
            "task": _event_reference(task_path),
            "production_version_id": context["production_version_id"],
            "production_manifest_sha256": context["production_manifest_sha256"],
            "event_stream": _event_reference(events_path),
            "selection": None,
            "started_at": started_at,
            "updated_at": started_at,
        }
        validate_instance(TRANSACTION_SCHEMA, transaction)
        write_new_json(root / "transaction.json", transaction)
        fsync_directory(root)
        fsync_directory(root.parent)
        committed = True
        return root, transaction, persisted_context
    finally:
        if not committed and root.exists():
            _safe_remove_transaction(root, root.parent)


def _write_transaction(root: Path, transaction: dict[str, Any]) -> None:
    validate_instance(TRANSACTION_SCHEMA, transaction)
    atomic_replace_json(root / "transaction.json", transaction)


def _update_process(root: Path, **updates: Any) -> None:
    process = load_json_object(root / "process.json")
    process.update(updates)
    atomic_replace_json(root / "process.json", process)


def _read_events(path: Path) -> tuple[bytes, list[dict[str, Any]]]:
    raw = path.read_bytes()
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UsageError(f"invalid CLI JSONL event at line {line_number}") from exc
        if not isinstance(value, dict):
            raise UsageError(f"CLI JSONL event {line_number} is not an object")
        events.append(value)
    return raw, events


def _persist_event(
    event_file: BinaryIO,
    line: bytes,
    root: Path,
    transaction: dict[str, Any],
    context: dict[str, Any],
    events: list[dict[str, Any]],
) -> EventInspection:
    try:
        value = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UsageError("CLI stdout contains invalid JSONL") from exc
    if not isinstance(value, dict):
        raise UsageError("CLI stdout JSONL event must be an object")
    event_file.write(line if line.endswith(b"\n") else line + b"\n")
    event_file.flush()
    os.fsync(event_file.fileno())
    events.append(value)
    inspection = _inspect_usage_events(context["cli_id"], events, context)
    transaction["event_stream"] = _event_reference(root / "events.jsonl")
    if inspection.selections and transaction["selection"] is None:
        first = min(inspection.selections, key=lambda item: item.event_index)
        if inspection.session_id is None:  # pragma: no cover - inspector enforces this
            raise UsageError("selected production Skill has no CLI session")
        transaction["state"] = "SELECTED"
        transaction["selection"] = {
            "session_id": inspection.session_id,
            "event_index": first.event_index,
            "loaded_sha256": first.candidate["skill_sha256"],
        }
    transaction["updated_at"] = utc_now()
    _write_transaction(root, transaction)
    return inspection


def _command(context: dict[str, Any], task: str) -> list[str]:
    if context["cli_id"] == "claude-code":
        return [
            context["executable"],
            "--plugin-dir",
            str(context["plugin_root"]),
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--permission-mode",
            "dontAsk",
            task,
        ]
    return [
        context["executable"],
        "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        task,
    ]


def _spawn_cli(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    stderr: BinaryIO,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        command_for_platform(command),
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=stderr,
        start_new_session=True,
    )


def _terminate_child(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _capture_cli(
    root: Path,
    transaction: dict[str, Any],
    context: dict[str, Any],
    task: str,
) -> tuple[list[dict[str, Any]], int | None, str | None]:
    environment = os.environ.copy()
    environment["PAL_LIBRARY_ROOT"] = str(context["library_root"])
    environment["PAL_PRODUCTION_VERSION_ID"] = context["production_version_id"]
    environment["PAL_USAGE_ID"] = transaction["usage_id"]
    if context["config_root"]:
        environment["PAL_CONFIG_ROOT"] = str(context["config_root"])
    events: list[dict[str, Any]] = []
    interrupted: str | None = None
    with (root / "stderr.log").open("wb") as stderr_file:
        process = _spawn_cli(
            _command(context, task),
            cwd=Path(context["library_root"]),
            environment=environment,
            stderr=stderr_file,
        )
        _update_process(root, child_pid=process.pid)
        if process.stdout is None:  # pragma: no cover - Popen contract
            raise UsageError("controlled CLI stdout pipe is unavailable")
        try:
            with (root / "events.jsonl").open("ab", buffering=0) as event_file:
                while True:
                    line = process.stdout.readline()
                    if not line:
                        break
                    _persist_event(event_file, line, root, transaction, context, events)
            exit_code = process.wait()
        except KeyboardInterrupt:
            interrupted = "SIGINT"
            _terminate_child(process)
            exit_code = None
        finally:
            if process.poll() is None:
                _terminate_child(process)
    _update_process(
        root,
        exited_at=utc_now(),
        process_exit_code=exit_code,
    )
    return events, exit_code, interrupted


def _interruption_output(usage_id: str, reason: str) -> str:
    return json.dumps(
        {
            "proof": "PAL_RUN_INTERRUPTED",
            "reason": reason,
            "usage_id": usage_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _outcome(
    inspection: EventInspection,
    usage_id: str,
    process_exit_code: int | None,
    interruption: str | None,
) -> dict[str, Any]:
    if not inspection.selections or inspection.session_id is None:
        raise UsageError("CLI run did not prove an active production Skill load")
    if interruption is not None:
        return {
            "status": "interrupted",
            "output": _interruption_output(usage_id, interruption),
            "process_exit_code": None,
            "error_code": "pal-run-interrupted",
            "completion_kind": "pal-recovery-envelope",
            "result_code": "interrupted",
            "signal": interruption,
        }
    if process_exit_code is not None and process_exit_code < 0:
        signal = f"signal-{abs(process_exit_code)}"
        return {
            "status": "interrupted",
            "output": _interruption_output(usage_id, signal),
            "process_exit_code": None,
            "error_code": "pal-run-interrupted",
            "completion_kind": "pal-recovery-envelope",
            "result_code": "interrupted",
            "signal": signal,
        }
    if inspection.terminal_status == "succeeded" and process_exit_code == 0:
        if inspection.output is None:
            raise UsageError("successful CLI run has no readable output")
        return {
            "status": "succeeded",
            "output": inspection.output,
            "process_exit_code": 0,
            "error_code": None,
            "completion_kind": "terminal-cli-event",
            "result_code": inspection.result_code or "completed",
            "signal": None,
        }
    if inspection.terminal_status == "failed" or (
        process_exit_code is not None and process_exit_code != 0
    ):
        output = inspection.output or json.dumps(
            {
                "proof": "PAL_CLI_TASK_FAILED",
                "process_exit_code": process_exit_code,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        result_code = inspection.result_code or f"process-exit-{process_exit_code}"
        return {
            "status": "failed",
            "output": output,
            "process_exit_code": process_exit_code,
            "error_code": "cli-task-failed",
            "completion_kind": "terminal-cli-event",
            "result_code": result_code,
            "signal": None,
        }
    reason = "missing-terminal-event"
    return {
        "status": "interrupted",
        "output": _interruption_output(usage_id, reason),
        "process_exit_code": None,
        "error_code": "pal-run-interrupted",
        "completion_kind": "pal-recovery-envelope",
        "result_code": "interrupted",
        "signal": reason,
    }


def _record_value(
    *,
    transaction: dict[str, Any],
    context: dict[str, Any],
    task: str,
    events_raw: bytes,
    events: list[dict[str, Any]],
    inspection: EventInspection,
    outcome: dict[str, Any],
    completed_at: str,
) -> dict[str, Any]:
    selections = sorted(inspection.selections, key=lambda item: item.event_index)
    first = selections[0]
    logical_units: list[dict[str, str]] = []
    loaded_artifacts: list[dict[str, Any]] = []
    seen_units: set[tuple[str, str]] = set()
    for selection in selections:
        candidate = selection.candidate
        unit_key = (candidate["unit_id"], candidate["release_id"])
        if unit_key not in seen_units:
            logical_units.append(
                {"unit_id": candidate["unit_id"], "release_id": candidate["release_id"]}
            )
            seen_units.add(unit_key)
        loaded_artifacts.append(
            {
                "artifact_id": candidate["artifact_id"],
                "profile_id": candidate["profile_id"],
                "covered_clis": candidate["covered_clis"],
                "sha256": candidate["tree_sha256"],
            }
        )
    output_sha256 = sha256_bytes(outcome["output"].encode("utf-8"))
    record = {
        "schema_version": 1,
        "usage_id": transaction["usage_id"],
        "task": {
            "task_id": context["task_id"],
            "input": task,
            "input_sha256": sha256_bytes(task.encode("utf-8")),
        },
        "overall_result": {
            "status": outcome["status"],
            "output": outcome["output"],
            "output_sha256": output_sha256,
            "process_exit_code": outcome["process_exit_code"],
            "error_code": outcome["error_code"],
        },
        "cli": {
            "id": context["cli_id"],
            "version": context["cli_version"],
            "session_id": inspection.session_id,
        },
        "production_version_id": context["production_version_id"],
        "logical_units": logical_units,
        "loaded_artifacts": loaded_artifacts,
        "evidence": {
            "selection": {
                "kind": "actual-skill-load",
                "event_index": first.event_index,
                "loaded_relative_path": first.candidate["relative_skill_path"],
                "loaded_sha256": first.candidate["skill_sha256"],
            },
            "completion": {
                "kind": outcome["completion_kind"],
                "result_code": outcome["result_code"],
                "process_exit_code": outcome["process_exit_code"],
                "signal": outcome["signal"],
                "output_sha256": output_sha256,
            },
            "capture": {
                "kind": "controlled-jsonl-runner",
                "event_stream_sha256": sha256_bytes(events_raw),
                "event_count": len(events),
                "mount_bundle_sha256": context["mount_bundle_sha256"],
                "production_manifest_sha256": context["production_manifest_sha256"],
                "recorder_version": __version__,
            },
        },
        "started_at": transaction["started_at"],
        "completed_at": completed_at,
    }
    validate_instance(RECORD_SCHEMA, record)
    return record


def _assert_no_p3_fields(value: Any, label: str = "record") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in FORBIDDEN_P3_KEYS:
                raise UsageError(f"P3 field is forbidden in v1 usage records: {label}.{key}")
            _assert_no_p3_fields(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_p3_fields(child, f"{label}[{index}]")


def _parse_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise UsageError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UsageError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise UsageError(f"{label} must include a timezone")
    return parsed


def _validate_record_value(
    record: dict[str, Any],
    library_root: Path,
    *,
    config_root: Path | None,
    record_path: Path | None,
) -> dict[str, Any]:
    validate_instance(RECORD_SCHEMA, record)
    _assert_no_p3_fields(record)
    usage_id = require_safe_id(record["usage_id"], "usage record ID")
    task = record["task"]
    if sha256_bytes(task["input"].encode("utf-8")) != task["input_sha256"]:
        raise IntegrityError("usage task input SHA-256 mismatch")
    overall = record["overall_result"]
    if sha256_bytes(overall["output"].encode("utf-8")) != overall["output_sha256"]:
        raise IntegrityError("usage result output SHA-256 mismatch")
    cli = record["cli"]
    if re.fullmatch(r"\d+\.\d+\.\d+", cli["version"]) is None:
        raise UsageError("usage record CLI version is invalid")

    validated = validate_production_version(library_root, record["production_version_id"])
    production = validated["production"]
    releases = {item["release_id"]: item["unit_id"] for item in production["releases"]}
    artifacts = {item["artifact_id"]: item for item in production["artifacts"]}
    if len(record["loaded_artifacts"]) != len(
        {item["artifact_id"] for item in record["loaded_artifacts"]}
    ):
        raise UsageError("usage record contains duplicate loaded artifacts")
    selected_artifacts: dict[str, dict[str, Any]] = {}
    for item in record["loaded_artifacts"]:
        artifact = artifacts.get(item["artifact_id"])
        if artifact is None:
            raise IntegrityError(f"usage artifact is absent from production: {item['artifact_id']}")
        expected = {
            "artifact_id": artifact["artifact_id"],
            "profile_id": artifact["profile_id"],
            "covered_clis": artifact["covered_clis"],
            "sha256": artifact["tree_sha256"],
        }
        if item != expected or cli["id"] not in artifact["covered_clis"]:
            raise IntegrityError(f"usage artifact closure mismatch: {item['artifact_id']}")
        selected_artifacts[item["artifact_id"]] = artifact
    expected_units = {
        (releases[artifact["release_id"]], artifact["release_id"])
        for artifact in selected_artifacts.values()
    }
    actual_units = {(item["unit_id"], item["release_id"]) for item in record["logical_units"]}
    if actual_units != expected_units or len(actual_units) != len(record["logical_units"]):
        raise IntegrityError("usage logical-unit/release closure mismatch")

    selection = record["evidence"]["selection"]
    relative = normalize_relative_path(
        selection["loaded_relative_path"],
        "usage selected Skill path",
    )
    matches: list[Path] = []
    version_root = validated["manifest_path"].parent
    for artifact in selected_artifacts.values():
        payload_root = require_inside(
            version_root,
            artifact["payload_root"],
            f"usage artifact {artifact['artifact_id']}",
        )
        candidate = payload_root / relative
        if candidate.is_file() and not is_link(candidate):
            matches.append(candidate)
    if len(matches) != 1 or sha256_file(matches[0]) != selection["loaded_sha256"]:
        raise IntegrityError("usage selected Skill does not resolve through production")
    capture = record["evidence"]["capture"]
    if capture["production_manifest_sha256"] != validated["manifest_sha256"]:
        raise IntegrityError("usage capture production manifest SHA-256 mismatch")
    if selection["event_index"] >= capture["event_count"]:
        raise UsageError("usage selection event index exceeds the captured stream")

    completion = record["evidence"]["completion"]
    if completion["output_sha256"] != overall["output_sha256"]:
        raise IntegrityError("usage completion and result output SHA-256 differ")
    status = overall["status"]
    if status == "succeeded":
        if (
            overall["process_exit_code"] != 0
            or overall["error_code"] is not None
            or completion["kind"] != "terminal-cli-event"
            or completion["process_exit_code"] != 0
            or completion["signal"] is not None
        ):
            raise UsageError("succeeded usage record has an invalid completion envelope")
    elif status == "failed":
        if (
            overall["error_code"] is None
            or completion["kind"] != "terminal-cli-event"
            or completion["signal"] is not None
            or completion["process_exit_code"] != overall["process_exit_code"]
        ):
            raise UsageError("failed usage record has an invalid completion envelope")
    elif (
        overall["process_exit_code"] is not None
        or overall["error_code"] != "pal-run-interrupted"
        or completion["kind"] != "pal-recovery-envelope"
        or completion["result_code"] != "interrupted"
        or completion["process_exit_code"] is not None
        or not completion["signal"]
    ):
        raise UsageError("interrupted usage record has an invalid recovery envelope")

    started = _parse_timestamp(record["started_at"], "usage started_at")
    completed = _parse_timestamp(record["completed_at"], "usage completed_at")
    if completed < started:
        raise UsageError("usage completed_at precedes started_at")
    if record_path is not None:
        completed_utc = completed.astimezone(UTC)
        expected_suffix = Path(
            f"{completed_utc.year:04d}/{completed_utc.month:02d}/{usage_id}.json"
        )
        if not record_path.is_relative_to(library_root / "records/usage") or not str(
            record_path
        ).endswith(str(expected_suffix)):
            raise PathSafetyError("usage record path does not match its ID and completion month")
    if config_root is not None:
        mount = validate_production_mount(
            library_root,
            record["production_version_id"],
            config_root=config_root,
        )
        if mount["bundle_sha256"] != capture["mount_bundle_sha256"]:
            raise IntegrityError("usage capture production mount bundle SHA-256 mismatch")
    return {
        "usage_id": usage_id,
        "status": status,
        "cli_id": cli["id"],
        "production_version_id": record["production_version_id"],
        "logical_unit_count": len(record["logical_units"]),
        "loaded_artifact_count": len(record["loaded_artifacts"]),
    }


def _record_parent(library_root: Path, completed_at: str) -> Path:
    completed = _parse_timestamp(completed_at, "usage completed_at").astimezone(UTC)
    root = require_inside(library_root, "records/usage", "usage record root")
    parent = root / f"{completed.year:04d}" / f"{completed.month:02d}"
    _mkdir_descendant(parent, library_root)
    return parent


def _cleanup_record_temps(parent: Path) -> list[Path]:
    removed: list[Path] = []
    for path in sorted(parent.iterdir()):
        if TEMP_RECORD_PATTERN.fullmatch(path.name) is None:
            continue
        if is_link(path) or not path.is_file():
            raise PathSafetyError(f"unsafe usage record temporary file: {path}")
        path.unlink()
        removed.append(path)
    if removed:
        fsync_directory(parent)
    return removed


@contextmanager
def _record_lock(parent: Path) -> Iterator[None]:
    if fcntl is None:  # pragma: no cover - P0 rejects this platform
        raise UsageError("usage record append requires a supported file lock backend")
    lock_path = parent / ".append.lock"
    if is_link(lock_path):
        raise PathSafetyError(f"usage append lock cannot be a symbolic link: {lock_path}")
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise UsageError(
                        f"timed out waiting for usage append lock: {lock_path}"
                    ) from exc
                time.sleep(0.01)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _append_record(library_root: Path, record: dict[str, Any]) -> Path:
    parent = _record_parent(library_root, record["completed_at"])
    target = parent / f"{record['usage_id']}.json"
    with _record_lock(parent):
        _cleanup_record_temps(parent)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(formatted_json_bytes(record))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise UsageError(
                    f"usage record already exists and is append-only: {target}"
                ) from exc
            fsync_directory(parent)
        finally:
            if temporary.exists():
                temporary.unlink()
        return target


def _complete_transaction(
    root: Path,
    transaction: dict[str, Any],
    record: dict[str, Any],
) -> Path:
    record_path = _append_record(root.parents[3], record)
    transaction["state"] = "COMPLETED"
    transaction["event_stream"] = _event_reference(root / "events.jsonl")
    transaction["updated_at"] = utc_now()
    _write_transaction(root, transaction)
    _safe_remove_transaction(root, root.parent)
    return record_path


@lifecycle_write
def run_production_task(
    library_root: Path,
    cli_id: str,
    task: str,
    *,
    config_root: Path | None = None,
) -> dict[str, Any]:
    if not isinstance(task, str) or not task.strip():
        raise UsageError("production task must be non-empty UTF-8 text")
    if "\x00" in task:
        raise UsageError("production task cannot contain a NUL character")
    with active_runtime_context(library_root, cli_id, config_root=config_root) as runtime:
        root, transaction, context = _create_transaction(runtime, task)
        try:
            events, process_exit_code, interruption = _capture_cli(
                root,
                transaction,
                context
                | {
                    "executable": runtime["executable"],
                    "library_root": runtime["library_root"],
                    "config_root": runtime["config_root"],
                    "plugin_root": runtime["plugin_root"],
                    "runtime_plugin_root": runtime["runtime_plugin_root"],
                },
                task,
            )
            events_raw = (root / "events.jsonl").read_bytes()
            inspection = _inspect_usage_events(cli_id, events, context)
            outcome = _outcome(
                inspection,
                transaction["usage_id"],
                process_exit_code,
                interruption,
            )
            completed_at = utc_now()
            record = _record_value(
                transaction=transaction,
                context=context,
                task=task,
                events_raw=events_raw,
                events=events,
                inspection=inspection,
                outcome=outcome,
                completed_at=completed_at,
            )
            _validate_record_value(
                record,
                runtime["library_root"],
                config_root=runtime["config_root"],
                record_path=None,
            )
            record_path = _complete_transaction(root, transaction, record)
            return {
                "usage_id": record["usage_id"],
                "task_id": record["task"]["task_id"],
                "status": record["overall_result"]["status"],
                "cli_id": cli_id,
                "cli_version": context["cli_version"],
                "compatibility_classification": runtime["cli_compatibility"]["classification"],
                "production_version_id": context["production_version_id"],
                "record": str(record_path),
                "record_sha256": sha256_file(record_path),
                "process_exit_code": record["overall_result"]["process_exit_code"],
            }
        except (UsageError, IntegrityError, PathSafetyError, OSError):
            if root.exists() and transaction.get("selection") is None:
                _safe_remove_transaction(root, root.parent)
            raise


def validate_usage_record(
    library_root: Path,
    record_path: Path,
    *,
    config_root: Path | None = None,
) -> dict[str, Any]:
    requested_root = Path(os.path.abspath(library_root))
    root = canonical_existing_root(library_root)
    records_root = require_inside(root, "records/usage", "usage record root")
    absolute = Path(os.path.abspath(record_path))
    relative: str | None = None
    for candidate_root in (requested_root / "records/usage", records_root):
        try:
            relative = absolute.relative_to(candidate_root).as_posix()
            break
        except ValueError:
            continue
    if relative is None:
        try:
            relative = absolute.resolve(strict=True).relative_to(records_root).as_posix()
        except (FileNotFoundError, ValueError) as exc:
            raise PathSafetyError(f"usage record escapes the library: {absolute}") from exc
    path = require_inside(records_root, relative, "usage record")
    record = load_json_object(path)
    resolved_config = resolve_config_root(config_root, create=False)
    result = _validate_record_value(
        record,
        root,
        config_root=resolved_config,
        record_path=path,
    )
    return {"record": str(path), "record_sha256": sha256_file(path), **result}


def _process_alive(process: dict[str, Any]) -> bool:
    if process.get("hostname") != socket.gethostname():
        return False
    for field in ("wrapper_pid", "child_pid"):
        pid = process.get(field)
        if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
            continue
        if process_exists(pid):
            return True
    return False


def _load_process(root: Path, transaction: dict[str, Any]) -> dict[str, Any]:
    process = load_json_object(root / "process.json")
    required = {
        "schema_version",
        "usage_id",
        "hostname",
        "wrapper_pid",
        "child_pid",
        "started_at",
        "exited_at",
        "process_exit_code",
    }
    if set(process) != required or process.get("schema_version") != 1:
        raise UsageError("usage process envelope fields are invalid")
    if process.get("usage_id") != transaction["usage_id"]:
        raise IntegrityError("usage process identity mismatch")
    if not isinstance(process.get("hostname"), str) or not process["hostname"]:
        raise UsageError("usage process hostname is invalid")
    wrapper_pid = process.get("wrapper_pid")
    child_pid = process.get("child_pid")
    if isinstance(wrapper_pid, bool) or not isinstance(wrapper_pid, int) or wrapper_pid <= 0:
        raise UsageError("usage wrapper PID is invalid")
    if child_pid is not None and (
        isinstance(child_pid, bool) or not isinstance(child_pid, int) or child_pid <= 0
    ):
        raise UsageError("usage child PID is invalid")
    if process.get("started_at") != transaction["started_at"]:
        raise IntegrityError("usage process and transaction start times differ")
    started = _parse_timestamp(process["started_at"], "usage process started_at")
    exited_value = process.get("exited_at")
    if exited_value is not None:
        exited = _parse_timestamp(exited_value, "usage process exited_at")
        if exited < started:
            raise UsageError("usage process exited_at precedes started_at")
    exit_code = process.get("process_exit_code")
    if isinstance(exit_code, bool) or (exit_code is not None and not isinstance(exit_code, int)):
        raise UsageError("usage process exit code is invalid")
    if exited_value is None and exit_code is not None:
        raise UsageError("usage process has an exit code without an exit timestamp")
    return process


def _find_existing_record(library_root: Path, usage_id: str) -> Path | None:
    matches = list((library_root / "records/usage").glob(f"*/*/{usage_id}.json"))
    if len(matches) > 1:
        raise IntegrityError(f"usage ID is duplicated across record months: {usage_id}")
    return matches[0] if matches else None


def _event_stream_extends_reference(path: Path, expected_sha256: str) -> bool:
    raw = path.read_bytes()
    digest = hashlib.sha256()
    if digest.hexdigest() == expected_sha256:
        return True
    for line in raw.splitlines(keepends=True):
        digest.update(line)
        if digest.hexdigest() == expected_sha256:
            return True
    return False


def _load_transaction(root: Path, library_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    transaction = load_json_object(root / "transaction.json")
    validate_instance(TRANSACTION_SCHEMA, transaction)
    if transaction["usage_id"] != root.name:
        raise IntegrityError("usage transaction directory identity mismatch")
    context = load_json_object(root / "context.json")
    _validate_context_shape(context, root, require_runtime=False)
    if context["library_root"] != str(library_root):
        raise IntegrityError("usage transaction library root mismatch")
    for field in ("usage_id", "library_id", "cli_id", "production_version_id"):
        if transaction[field] != context[field]:
            raise IntegrityError(f"usage transaction {field} differs from captured context")
    if transaction["production_manifest_sha256"] != context["production_manifest_sha256"]:
        raise IntegrityError("usage transaction production manifest digest mismatch")
    task_path = require_inside(root, transaction["task"]["path"], "usage transaction task")
    if task_path.name != "task.txt":
        raise IntegrityError("usage transaction task reference is not canonical")
    if sha256_file(task_path) != transaction["task"]["sha256"]:
        raise IntegrityError("usage transaction task SHA-256 mismatch")
    event_path = require_inside(
        root,
        transaction["event_stream"]["path"],
        "usage transaction event stream",
    )
    if event_path.name != "events.jsonl" or not _event_stream_extends_reference(
        event_path, transaction["event_stream"]["sha256"]
    ):
        raise IntegrityError("usage transaction event stream does not extend its durable prefix")
    state = transaction["state"]
    selection = transaction["selection"]
    if (state == "OPEN" and selection is not None) or (
        state in {"SELECTED", "COMPLETED"} and selection is None
    ):
        raise IntegrityError("usage transaction state and selection are inconsistent")
    return transaction, context


def _validate_recovery_context(context: dict[str, Any], library_root: Path) -> None:
    validated = validate_production_version(
        library_root,
        context["production_version_id"],
    )
    if validated["manifest_sha256"] != context["production_manifest_sha256"]:
        raise IntegrityError("recovery production manifest SHA-256 mismatch")
    config_root = resolve_config_root(Path(context["config_root"]), create=False)
    mount = validate_production_mount(
        library_root,
        context["production_version_id"],
        config_root=config_root,
    )
    if mount["bundle_sha256"] != context["mount_bundle_sha256"]:
        raise IntegrityError("recovery mount bundle SHA-256 mismatch")
    cli_id = context["cli_id"]
    expected_bundle = Path(mount["bundle"])
    expected_cli_mount = expected_bundle.parent / f"{cli_id}.json"
    expected_projection = Path(mount["projection_roots"][cli_id])
    expected_plugin = expected_projection / (
        f"plugins/{mount['plugin_name']}"
        if cli_id == "claude-code"
        else f"marketplace/plugins/{mount['plugin_name']}"
    )
    if Path(context["mount_bundle_path"]) != expected_bundle:
        raise IntegrityError("recovery mount bundle path mismatch")
    if (
        Path(context["cli_mount_path"]) != expected_cli_mount
        or sha256_file(expected_cli_mount) != context["cli_mount_sha256"]
    ):
        raise IntegrityError("recovery CLI mount closure mismatch")
    if (
        Path(context["projection_root"]) != expected_projection
        or context["projection_tree_sha256"] != mount["projection_tree_sha256"][cli_id]
        or Path(context["plugin_root"]) != expected_plugin
    ):
        raise IntegrityError("recovery production projection closure mismatch")
    if context["plugin_name"] != mount["plugin_name"] or context["marketplace_name"] != (
        mount["marketplace_name"] if cli_id == "codex" else None
    ):
        raise IntegrityError("recovery production plugin identity mismatch")

    production = validated["production"]
    units_by_release = {item["release_id"]: item["unit_id"] for item in production["releases"]}
    artifacts = {
        item["artifact_id"]: item
        for item in production["artifacts"]
        if cli_id in item["covered_clis"]
    }
    if set(artifacts) != {candidate["artifact_id"] for candidate in context["skills"]}:
        raise IntegrityError("recovery Skill candidates differ from production coverage")
    for candidate in context["skills"]:
        artifact = artifacts[candidate["artifact_id"]]
        expected_identity = {
            "unit_id": units_by_release[artifact["release_id"]],
            "release_id": artifact["release_id"],
            "profile_id": artifact["profile_id"],
            "covered_clis": artifact["covered_clis"],
            "tree_sha256": artifact["tree_sha256"],
        }
        if any(candidate[field] != value for field, value in expected_identity.items()):
            raise IntegrityError(
                f"recovery Skill identity differs from production: {candidate['artifact_id']}"
            )
        payload_root = require_inside(
            validated["manifest_path"].parent,
            artifact["payload_root"],
            f"recovery artifact {artifact['artifact_id']}",
        )
        source_skill = require_inside(
            payload_root,
            candidate["relative_skill_path"],
            f"recovery Skill {candidate['skill_name']}",
        )
        if sha256_file(source_skill) != candidate["skill_sha256"]:
            raise IntegrityError(f"recovery production Skill drifted: {source_skill}")
        projected = Path(candidate["projected_skill_path"])
        if is_link(projected) or not projected.is_file():
            raise IntegrityError(f"recovery projected Skill is unavailable: {projected}")
        if sha256_file(projected) != candidate["skill_sha256"]:
            raise IntegrityError(f"recovery projected Skill drifted: {projected}")


@lifecycle_write
def recover_usage_transactions(
    library_root: Path,
) -> dict[str, Any]:
    root = canonical_existing_root(library_root)
    doctor_development_context(root)
    parent = require_inside(
        root,
        ".pal/transactions/usage",
        "usage transaction parent",
    )
    recovered: list[str] = []
    aborted: list[str] = []
    pending: list[str] = []
    for transaction_root in sorted(parent.iterdir()):
        if is_link(transaction_root) or not transaction_root.is_dir():
            raise PathSafetyError(f"invalid usage transaction entry: {transaction_root}")
        require_safe_id(transaction_root.name, "usage transaction directory")
        transaction, context = _load_transaction(transaction_root, root)
        existing = _find_existing_record(root, transaction["usage_id"])
        if existing is not None:
            validate_usage_record(
                root,
                existing,
                config_root=Path(context["config_root"]),
            )
            _safe_remove_transaction(transaction_root, parent)
            recovered.append(transaction["usage_id"])
            continue
        process = _load_process(transaction_root, transaction)
        if _process_alive(process):
            pending.append(transaction["usage_id"])
            continue
        _validate_recovery_context(context, root)
        events_raw, events = _read_events(transaction_root / "events.jsonl")
        inspection = _inspect_usage_events(
            context["cli_id"],
            events,
            context,
            verify_runtime=False,
        )
        if not inspection.selections:
            transaction["state"] = "ABORTED"
            transaction["event_stream"] = _event_reference(transaction_root / "events.jsonl")
            transaction["updated_at"] = utc_now()
            _write_transaction(transaction_root, transaction)
            _safe_remove_transaction(transaction_root, parent)
            aborted.append(transaction["usage_id"])
            continue
        task = (transaction_root / "task.txt").read_text(encoding="utf-8")
        process_exit_code = process.get("process_exit_code")
        has_durable_exit = (
            isinstance(process_exit_code, int) and process.get("exited_at") is not None
        )
        outcome = _outcome(
            inspection,
            transaction["usage_id"],
            process_exit_code if has_durable_exit else None,
            None if has_durable_exit else "wrapper-recovery",
        )
        record = _record_value(
            transaction=transaction,
            context=context,
            task=task,
            events_raw=events_raw,
            events=events,
            inspection=inspection,
            outcome=outcome,
            completed_at=utc_now(),
        )
        _validate_record_value(
            record,
            root,
            config_root=Path(context["config_root"]),
            record_path=None,
        )
        _complete_transaction(transaction_root, transaction, record)
        recovered.append(transaction["usage_id"])
    return {
        "recovered_usage_ids": recovered,
        "aborted_usage_ids": aborted,
        "pending_usage_ids": pending,
    }


__all__ = [
    "recover_usage_transactions",
    "run_production_task",
    "validate_usage_record",
]
