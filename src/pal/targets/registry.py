"""Built-in target-driver registry."""

from __future__ import annotations

from ..errors import CompatibilityError
from .base import TargetDriver
from .claude_code import ClaudeCodeTargetDriver
from .codex import CodexTargetDriver

_DRIVERS: dict[str, TargetDriver] = {
    driver.target_id: driver for driver in (ClaudeCodeTargetDriver(), CodexTargetDriver())
}


def target_driver(target_id: object) -> TargetDriver:
    if not isinstance(target_id, str) or target_id not in _DRIVERS:
        raise CompatibilityError(f"target driver is not registered: {target_id}")
    return _DRIVERS[target_id]


def registered_target_contracts() -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "target_id": target_id,
            "executable": driver.executable_name,
            "minimum_version": driver.minimum_version,
            "verified_versions": sorted(driver.verified_versions),
            "incompatible_versions": sorted(driver.incompatible_versions),
        }
        for target_id, driver in sorted(_DRIVERS.items())
    )


__all__ = ["registered_target_contracts", "target_driver"]
