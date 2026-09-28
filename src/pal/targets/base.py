"""Contracts for Claude Code/Codex target drivers and their stable probes."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import CompatibilityError, UsageError


@dataclass(frozen=True)
class CommandProbe:
    """One read-only target command contract checked before a CLI is admitted."""

    probe_id: str
    arguments: tuple[str, ...]
    required_fragments: tuple[str, ...] = ()
    json_envelope: str | None = None
    json_array: bool = False


class TargetDriver(ABC):
    """A target CLI contract; target plugins are projections, never PAL facts."""

    target_id: str
    executable_name: str
    minimum_version: str
    verified_versions: frozenset[str]
    incompatible_versions: frozenset[str]

    @abstractmethod
    def compatibility_probes(self) -> tuple[CommandProbe, ...]:
        """Return stable command/JSON probes needed before using this target."""

    @abstractmethod
    def project_plugin(
        self,
        *,
        plugin_name: str,
        marketplace_name: str | None,
        plugin_version: str,
        description: str,
        marketplace_display_name: str,
        display_name: str,
        short_description: str,
        long_description: str,
        default_prompt: str,
        payload_files: dict[str, bytes],
    ) -> tuple[str, dict[str, bytes]]:
        """Wrap neutral component payloads in one native target plugin."""

    def validate_probe_output(
        self,
        probe: CommandProbe,
        *,
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> str:
        if returncode != 0:
            detail = stderr.strip() or stdout.strip() or f"exit {returncode}"
            raise CompatibilityError(
                f"{self.target_id} compatibility probe {probe.probe_id} failed: {detail}"
            )
        output = f"{stdout}\n{stderr}"
        missing = [item for item in probe.required_fragments if item not in output]
        if missing:
            raise CompatibilityError(
                f"{self.target_id} compatibility probe {probe.probe_id} is missing: "
                f"{', '.join(missing)}"
            )
        if probe.json_array:
            try:
                entries = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise CompatibilityError(
                    f"{self.target_id} compatibility probe {probe.probe_id} returned invalid JSON"
                ) from exc
            if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
                raise CompatibilityError(
                    f"{self.target_id} compatibility probe {probe.probe_id} returned "
                    "invalid JSON entries"
                )
            return f"array={len(entries)}"
        if probe.json_envelope is not None:
            try:
                payload = json.loads(stdout)
                entries = payload[probe.json_envelope]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise CompatibilityError(
                    f"{self.target_id} compatibility probe {probe.probe_id} returned "
                    "an invalid JSON envelope"
                ) from exc
            if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
                raise CompatibilityError(
                    f"{self.target_id} compatibility probe {probe.probe_id} returned "
                    "invalid JSON entries"
                )
            return f"{probe.json_envelope}={len(entries)}"
        return "required command surface present"

    def project_install_root(
        self,
        *,
        plugin_name: str,
        marketplace_name: str,
        description: str,
        projection_files: Mapping[str, bytes],
    ) -> dict[str, bytes]:
        """Return a persistent-install marketplace root, or ``{}`` when unsupported.

        The root is materialized beside the immutable projection instead of inside
        it, so adding persistent installation never rewrites already-activated
        projection bytes or the digests recorded in their mount bundles.
        """

        del plugin_name, marketplace_name, description, projection_files
        return {}

    def validate_runtime_initialization(
        self,
        init: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[str, ...]:
        """Validate target runtime initialization and return observed capabilities."""

        return ()


def normalized_capabilities(value: object) -> tuple[str, ...]:
    """Normalize the documented open string array without assuming order."""

    if isinstance(value, list) and all(isinstance(item, str) and item for item in value):
        return tuple(sorted(set(value)))
    raise UsageError("Claude system/init capabilities has an unsupported shape")


def target_plugin_error_mentions(error: object, plugin_name: str, plugin_root: Path) -> bool:
    if not isinstance(error, dict):
        raise UsageError("Claude system/init plugin_errors contains an invalid entry")
    fields = {field: error.get(field) for field in ("plugin", "type", "message")}
    if not all(isinstance(value, str) and value for value in fields.values()):
        raise UsageError("Claude system/init plugin_errors contains an invalid entry")
    material = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    return plugin_name in material or str(plugin_root) in material


__all__ = [
    "CommandProbe",
    "TargetDriver",
    "normalized_capabilities",
    "target_plugin_error_mentions",
]
