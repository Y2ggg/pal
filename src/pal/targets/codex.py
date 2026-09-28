"""Codex target contract using stable commands and JSON envelopes."""

from __future__ import annotations

from ..errors import IntegrityError
from ..io import formatted_json_bytes
from .base import CommandProbe, TargetDriver


class CodexTargetDriver(TargetDriver):
    """Project the PAL 0.1 compatibility layout accepted by Codex.

    Current OpenAI guidance prefers a portable root ``plugin.json``. Existing
    immutable PAL projections keep the documented ``.codex-plugin`` fallback
    until a versioned target-projection migration can preserve old mount bytes.
    """

    target_id = "codex"
    executable_name = "codex"
    minimum_version = "0.147.0"
    verified_versions = frozenset({"0.147.0", "0.154.0"})
    incompatible_versions = frozenset()

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
        if not marketplace_name:
            raise IntegrityError("Codex target projection requires a marketplace name")
        plugin_relative = f"marketplace/plugins/{plugin_name}"
        files = {
            "marketplace/.agents/plugins/marketplace.json": formatted_json_bytes(
                {
                    "name": marketplace_name,
                    "interface": {"displayName": marketplace_display_name},
                    "plugins": [
                        {
                            "name": plugin_name,
                            "source": {
                                "source": "local",
                                "path": f"./plugins/{plugin_name}",
                            },
                            "policy": {
                                "installation": "AVAILABLE",
                                "authentication": "ON_INSTALL",
                            },
                            "category": "Productivity",
                        }
                    ],
                }
            ),
            f"{plugin_relative}/.codex-plugin/plugin.json": formatted_json_bytes(
                {
                    "name": plugin_name,
                    "version": plugin_version,
                    "description": description,
                    "author": {"name": "PAL"},
                    "skills": "./skills/",
                    "interface": {
                        "displayName": display_name,
                        "shortDescription": short_description,
                        "longDescription": long_description,
                        "developerName": "PAL",
                        "category": "Productivity",
                        "capabilities": [],
                        "defaultPrompt": default_prompt,
                    },
                }
            ),
        }
        for relative, material in payload_files.items():
            destination = f"{plugin_relative}/{relative}"
            if destination in files:
                raise IntegrityError(f"component payload conflicts with Codex metadata: {relative}")
            files[destination] = material
        return plugin_relative, files

    def compatibility_probes(self) -> tuple[CommandProbe, ...]:
        return (
            CommandProbe("cli-help", ("--help",), ("plugin", "exec")),
            CommandProbe("exec-json-help", ("exec", "--help"), ("--json", "--ephemeral")),
            CommandProbe(
                "plugin-list-help",
                ("plugin", "list", "--help"),
                ("--json",),
            ),
            CommandProbe(
                "plugin-add-help",
                ("plugin", "add", "--help"),
                ("--json", "PLUGIN[@MARKETPLACE]"),
            ),
            CommandProbe(
                "plugin-remove-help",
                ("plugin", "remove", "--help"),
                ("--json", "PLUGIN[@MARKETPLACE]"),
            ),
            CommandProbe(
                "marketplace-list-help",
                ("plugin", "marketplace", "list", "--help"),
                ("--json",),
            ),
            CommandProbe(
                "marketplace-add-help",
                ("plugin", "marketplace", "add", "--help"),
                ("--json", "<SOURCE>"),
            ),
            CommandProbe(
                "marketplace-remove-help",
                ("plugin", "marketplace", "remove", "--help"),
                ("--json", "<MARKETPLACE_NAME>"),
            ),
            CommandProbe(
                "plugin-list-json",
                ("plugin", "list", "--json"),
                json_envelope="installed",
            ),
            CommandProbe(
                "marketplace-list-json",
                ("plugin", "marketplace", "list", "--json"),
                json_envelope="marketplaces",
            ),
        )


__all__ = ["CodexTargetDriver"]
