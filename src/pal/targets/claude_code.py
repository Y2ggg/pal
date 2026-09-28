"""Claude Code target contract and runtime capability inspection."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..errors import IntegrityError, UsageError
from ..io import formatted_json_bytes
from .base import (
    CommandProbe,
    TargetDriver,
    normalized_capabilities,
    target_plugin_error_mentions,
)


class ClaudeCodeTargetDriver(TargetDriver):
    target_id = "claude-code"
    executable_name = "claude"
    minimum_version = "2.1.205"
    verified_versions = frozenset({"2.1.234"})
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
        del (
            marketplace_name,
            marketplace_display_name,
            display_name,
            short_description,
            long_description,
            default_prompt,
        )
        plugin_relative = f"plugins/{plugin_name}"
        files = {
            f"{plugin_relative}/.claude-plugin/plugin.json": formatted_json_bytes(
                {
                    "name": plugin_name,
                    "version": plugin_version,
                    "description": description,
                    "author": {"name": "PAL"},
                }
            )
        }
        for relative, material in payload_files.items():
            destination = f"{plugin_relative}/{relative}"
            if destination in files:
                raise IntegrityError(
                    f"component payload conflicts with Claude metadata: {relative}"
                )
            files[destination] = material
        return plugin_relative, files

    def project_install_root(
        self,
        *,
        plugin_name: str,
        marketplace_name: str,
        description: str,
        projection_files: Mapping[str, bytes],
    ) -> dict[str, bytes]:
        """Wrap the session projection as a local Claude marketplace root.

        Claude resolves a plugin ``source`` relative to the marketplace root, so
        the manifest and the plugin payload must share one real directory.  The
        payload is copied verbatim from the immutable projection, keeping both
        trees byte-identical without touching the projection itself.
        """

        prefix = f"plugins/{plugin_name}/"
        files = {
            ".claude-plugin/marketplace.json": formatted_json_bytes(
                {
                    "name": marketplace_name,
                    # ``claude plugin validate --strict`` treats a missing
                    # marketplace description as an error.
                    "description": description,
                    "owner": {"name": "PAL"},
                    "plugins": [
                        {
                            "name": plugin_name,
                            "source": f"./plugins/{plugin_name}",
                            "description": description,
                        }
                    ],
                }
            )
        }
        for relative, material in projection_files.items():
            if not relative.startswith(prefix):
                raise IntegrityError(
                    f"Claude projection file is outside the plugin root: {relative}"
                )
            files[relative] = material
        return files

    def compatibility_probes(self) -> tuple[CommandProbe, ...]:
        return (
            CommandProbe(
                "cli-help",
                ("--help",),
                (
                    "--plugin-dir",
                    "--print",
                    "--output-format",
                    "stream-json",
                    "--verbose",
                    "--no-session-persistence",
                    "--permission-mode",
                ),
            ),
            CommandProbe(
                "plugin-validate-help",
                ("plugin", "validate", "--help"),
                ("--strict",),
            ),
            CommandProbe(
                "plugin-marketplace-add-help",
                ("plugin", "marketplace", "add", "--help"),
                ("--scope",),
            ),
            CommandProbe(
                "plugin-install-help",
                ("plugin", "install", "--help"),
                ("--scope",),
            ),
            CommandProbe(
                "plugin-uninstall-help",
                ("plugin", "uninstall", "--help"),
                (),
            ),
            CommandProbe(
                "plugin-list-json",
                ("plugin", "list", "--json"),
                json_array=True,
            ),
            CommandProbe(
                "plugin-marketplace-list-json",
                ("plugin", "marketplace", "list", "--json"),
                json_array=True,
            ),
        )

    def validate_runtime_initialization(
        self,
        init: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[str, ...]:
        if init.get("claude_code_version") != context["cli_version"]:
            raise UsageError("Claude init version differs from the probed runtime version")
        capabilities = normalized_capabilities(init.get("capabilities"))
        expected_root = Path(context["plugin_root"]).resolve(strict=True)
        plugins = init.get("plugins")
        if not isinstance(plugins, list):
            raise UsageError("Claude system/init plugins has an unsupported shape")
        matching = []
        for plugin in plugins:
            if not isinstance(plugin, dict) or plugin.get("name") != context["plugin_name"]:
                continue
            path_value = plugin.get("path")
            if (
                isinstance(path_value, str)
                and Path(path_value).resolve(strict=True) == expected_root
            ):
                matching.append(plugin)
        if len(matching) != 1:
            raise UsageError("Claude init did not load the active production plugin")
        # The documented init contract omits ``plugin_errors`` when empty.
        # Reject malformed collections and any target-specific error.
        errors = init.get("plugin_errors", [])
        if not isinstance(errors, list):
            raise UsageError("Claude system/init plugin_errors has an unsupported shape")
        if any(
            target_plugin_error_mentions(error, context["plugin_name"], expected_root)
            for error in errors
        ):
            raise UsageError("Claude system/init reports an error for the production plugin")
        return capabilities


__all__ = ["ClaudeCodeTargetDriver"]
