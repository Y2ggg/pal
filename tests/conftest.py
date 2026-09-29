"""Test-wide guards against mutating the developer's real CLI installations.

The production mount drives persistent Claude Code and Codex plugin installation
through the official CLIs.  A test that forgets to patch those seams would edit
the machine's real user-scope plugin state instead of a temporary fixture, so the
mutating subcommands are blocked here by default and must be opted into
explicitly.

Traceability: PRD-MOUNT-003, PRD-MOUNT-004; ACC-008, ACC-012.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, Sequence
from typing import Any

import pytest

BLOCKED_TARGETS = ("claude", "codex")
BLOCKED_SUBCOMMANDS = (
    ("plugin", "install"),
    ("plugin", "uninstall"),
    ("plugin", "add"),
    ("plugin", "remove"),
    ("plugin", "marketplace", "add"),
    ("plugin", "marketplace", "remove"),
)


def _executable_name(command: Any) -> str:
    if isinstance(command, str):
        return (
            command.replace("\\", "/")
            .rsplit("/", 1)[-1]
            .lower()
            .removesuffix(".exe")
            .removesuffix(".cmd")
        )
    if isinstance(command, Sequence) and command:
        return _executable_name(command[0])
    return ""


def _is_blocked(command: Any) -> tuple[str, ...] | None:
    # Native Windows npm wrappers are launched via node without cmd.exe.
    if (
        isinstance(command, Sequence)
        and not isinstance(command, str)
        and len(command) > 1
        and _executable_name(command) == "node"
    ):
        entry = str(command[1]).replace("\\", "/")
        if "/node_modules/@openai/codex/" in entry:
            command = ["codex", *command[2:]]
        elif "/node_modules/@anthropic-ai/claude-code/" in entry:
            command = ["claude", *command[2:]]
    if _executable_name(command) not in BLOCKED_TARGETS:
        return None
    if not isinstance(command, Sequence) or isinstance(command, str):
        return None
    arguments = tuple(str(item) for item in command[1:])
    for subcommand in BLOCKED_SUBCOMMANDS:
        if arguments[: len(subcommand)] == subcommand:
            return subcommand
    return None


@pytest.fixture(autouse=True)
def block_real_target_cli_mutations(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Fail any unpatched attempt to install or remove a real target plugin."""

    if request.node.get_closest_marker("allow_real_cli_mutation") is not None:
        yield
        return

    original_run = subprocess.run
    original_popen = subprocess.Popen

    def guarded_run(command: Any, *args: Any, **kwargs: Any) -> Any:
        subcommand = _is_blocked(command)
        if subcommand is not None:
            raise AssertionError(
                "test attempted to mutate a real target CLI installation: "
                f"{_executable_name(command)} {' '.join(subcommand)}"
            )
        return original_run(command, *args, **kwargs)

    def guarded_popen(command: Any, *args: Any, **kwargs: Any) -> Any:
        subcommand = _is_blocked(command)
        if subcommand is not None:
            raise AssertionError(
                "test attempted to mutate a real target CLI installation: "
                f"{_executable_name(command)} {' '.join(subcommand)}"
            )
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    yield


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "allow_real_cli_mutation: permit a test to change real CLI plugin state",
    )
