"""Library-level target sets and coverage helpers.

Traceability: PRD-CLI-001 through PRD-CLI-003, PRD-CREATE-001, PRD-MOUNT-003;
ACC-003, ACC-004, ACC-008, ACC-012. The supported set is three targets; older
records may carry the two-target legacy set and remain readable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from .errors import IntegrityError
from .schema_catalog import LEGACY_TARGET_CLIS, TARGET_CLI_SETS, TARGET_CLIS


def allowed_target_sets() -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(item) for item in TARGET_CLI_SETS)


def normalize_target_clis(values: object, label: str = "target_clis") -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or not all(isinstance(item, str) for item in values):
        raise IntegrityError(f"{label} must be a string array")
    normalized = tuple(values)
    if normalized not in allowed_target_sets():
        raise IntegrityError(f"{label} is not a supported target set: {list(normalized)}")
    return normalized


def current_target_clis() -> tuple[str, ...]:
    return tuple(TARGET_CLIS)


def legacy_target_clis() -> tuple[str, ...]:
    return tuple(LEGACY_TARGET_CLIS)


def is_legacy_target_set(values: Sequence[str]) -> bool:
    return tuple(values) == legacy_target_clis()


def covered_targets(artifacts: Iterable[dict[str, Any]]) -> set[str]:
    covered: set[str] = set()
    for artifact in artifacts:
        for cli_id in artifact["covered_clis"]:
            covered.add(cli_id)
    return covered


def require_complete_coverage(
    artifacts: Iterable[dict[str, Any]],
    target_clis: Sequence[str],
    label: str,
) -> None:
    expected = set(target_clis)
    actual = covered_targets(artifacts)
    missing = sorted(expected - actual)
    if missing:
        raise IntegrityError(f"{label} does not cover targets: {', '.join(missing)}")


__all__ = [
    "allowed_target_sets",
    "covered_targets",
    "current_target_clis",
    "is_legacy_target_set",
    "legacy_target_clis",
    "normalize_target_clis",
    "require_complete_coverage",
]
