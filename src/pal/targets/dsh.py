"""DeepSeek Harness target contract using its filesystem skill root.

Traceability: PRD-CLI-001 through PRD-CLI-003, PRD-MOUNT-003;
ACC-003, ACC-004, ACC-008, ACC-012. DSH has no command executable or
marketplace; its consumption surface is a directory bundle under the skill
root. The driver only describes the projection; PAL-owned installation and
verification live in ``dsh_projection``.
"""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

from ..errors import CompatibilityError
from .base import CommandProbe, TargetDriver


def resolve_dsh_home(explicit: Path | None = None) -> Path:
    """Resolve the DSH config home without creating it or reading user config."""
    if explicit is not None:
        return Path(os.path.abspath(explicit))
    configured = os.environ.get("DSH_HOME")
    if configured:
        return Path(os.path.abspath(configured))
    return Path.home() / ".dsh"


def dsh_skill_root(home: Path) -> Path:
    return home / "skills"


def dsh_installed(home: Path) -> bool:
    return home.is_dir()


def detect_dsh_version() -> str:
    """Return the macOS app version when available; otherwise a stable marker."""
    plist = Path("/Applications/DeepSeek Harness.app/Contents/Info.plist")
    try:
        with plist.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, ValueError):
        return "0.2.2"
    version = payload.get("CFBundleShortVersionString")
    if not isinstance(version, str) or not version:
        return "0.2.2"
    # Compatibility floors are numeric; keep the descriptive app version out of
    # arithmetic while still reporting a stable value for mount records.
    return version


class DshTargetDriver(TargetDriver):
    target_id = "dsh"
    executable_name = ""
    minimum_version = "0.2.0"
    verified_versions = frozenset({"0.2.2-rc.5"})
    incompatible_versions = frozenset()

    def compatibility_probes(self) -> tuple[CommandProbe, ...]:
        return ()

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
            plugin_name,
            marketplace_name,
            plugin_version,
            description,
            marketplace_display_name,
            display_name,
            short_description,
            long_description,
            default_prompt,
        )
        # Payload files already use ``skills/<name>/...``; DSH discovers exactly
        # that layout and has no plugin manifest to add.
        return "", dict(payload_files)

    def require_root(self, home: Path) -> None:
        if not dsh_installed(home):
            raise CompatibilityError(f"DeepSeek Harness home is not configured: {home}")
        root = dsh_skill_root(home)
        if root.exists() and not root.is_dir():
            raise CompatibilityError(f"DeepSeek Harness skill root is not a directory: {root}")


__all__ = [
    "DshTargetDriver",
    "detect_dsh_version",
    "dsh_installed",
    "dsh_skill_root",
    "resolve_dsh_home",
]
