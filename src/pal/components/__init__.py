"""Trusted plugin-component contracts registered by this PAL build."""

from __future__ import annotations

from typing import Any

from .base import ComponentTypeDriver, PayloadFile, ValidatedCandidate
from .registry import ComponentTypeRegistry
from .skill import (
    SKILL_COMPONENT_TYPE_ID,
    SKILL_CONTRACT_ID,
    SKILL_KIND_ID,
    SkillComponentDriver,
)

_BUILTIN_REGISTRY = ComponentTypeRegistry([SkillComponentDriver()])


def component_type_driver(kind_id: object) -> ComponentTypeDriver:
    return _BUILTIN_REGISTRY.require_type(kind_id)


def component_type_driver_for_profile(profile: dict[str, Any]) -> ComponentTypeDriver:
    return _BUILTIN_REGISTRY.require_profile(profile)


def registered_component_contracts() -> tuple[dict[str, str], ...]:
    return _BUILTIN_REGISTRY.contracts()


__all__ = [
    "ComponentTypeDriver",
    "ComponentTypeRegistry",
    "PayloadFile",
    "SKILL_COMPONENT_TYPE_ID",
    "SKILL_CONTRACT_ID",
    "SKILL_KIND_ID",
    "SkillComponentDriver",
    "ValidatedCandidate",
    "component_type_driver",
    "component_type_driver_for_profile",
    "registered_component_contracts",
]
