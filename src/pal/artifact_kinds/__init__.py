"""PAL format-v1 compatibility aliases for the former artifact-kind API.

New lifecycle code imports :mod:`pal.components`.  These
aliases keep source compatibility for integrations written against PAL 0.1.0;
they do not define a second registry.
"""

from __future__ import annotations

from ..components import (
    SKILL_CONTRACT_ID,
    SKILL_KIND_ID,
    ComponentTypeDriver,
    ComponentTypeRegistry,
    PayloadFile,
    SkillComponentDriver,
    ValidatedCandidate,
    component_type_driver,
    component_type_driver_for_profile,
    registered_component_contracts,
)

ArtifactKindDriver = ComponentTypeDriver
ArtifactKindRegistry = ComponentTypeRegistry
SkillArtifactKindDriver = SkillComponentDriver
artifact_kind_driver = component_type_driver
artifact_kind_driver_for_profile = component_type_driver_for_profile


def registered_artifact_kind_contracts() -> tuple[dict[str, str], ...]:
    """Return the historical PAL 0.1.0 registry envelope."""

    return tuple(
        {"kind_id": item["type_id"], "contract_id": item["contract_id"]}
        for item in registered_component_contracts()
    )


__all__ = [
    "ArtifactKindDriver",
    "ArtifactKindRegistry",
    "PayloadFile",
    "SKILL_CONTRACT_ID",
    "SKILL_KIND_ID",
    "SkillArtifactKindDriver",
    "ValidatedCandidate",
    "artifact_kind_driver",
    "artifact_kind_driver_for_profile",
    "registered_artifact_kind_contracts",
]
