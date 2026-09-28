"""Target-neutral logical Plugin and component domain values.

PAL format 1.0 persists one Skill component through historical ``unit`` and
``artifact`` fields.  The codec below isolates that limitation.  The domain
model itself supports multiple independently typed components, so adding MCP
or Hook contracts does not redefine Plugin identity or lifecycle ownership.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import IntegrityError
from .schema_catalog import TARGET_CLIS


@dataclass(frozen=True)
class ComponentVariant:
    variant_id: str
    profile_id: str
    covered_targets: tuple[str, ...]


@dataclass(frozen=True)
class PluginComponent:
    component_id: str
    type_id: str
    variants: tuple[ComponentVariant, ...]


@dataclass(frozen=True)
class LogicalPlugin:
    plugin_id: str
    components: tuple[PluginComponent, ...]

    def __post_init__(self) -> None:
        component_ids = [component.component_id for component in self.components]
        if not self.plugin_id or not self.components:
            raise IntegrityError("logical Plugin identity and components must be non-empty")
        if len(component_ids) != len(set(component_ids)):
            raise IntegrityError("logical Plugin contains duplicate component IDs")


def decode_format_v1_plugin(
    plugin_id: str,
    artifacts: list[dict[str, Any]],
) -> LogicalPlugin:
    """Decode the single-component PAL v1 representation into the neutral domain."""

    if not artifacts:
        raise IntegrityError("format-v1 logical Plugin has no component variants")
    types = {artifact.get("kind") for artifact in artifacts}
    if len(types) != 1 or not all(isinstance(item, str) and item for item in types):
        raise IntegrityError("format-v1 logical Plugin must encode exactly one component type")
    covered = [target for artifact in artifacts for target in artifact["covered_clis"]]
    if covered != TARGET_CLIS:
        raise IntegrityError(
            "format-v1 component variant coverage must be disjoint and target-complete"
        )
    variants = tuple(
        ComponentVariant(
            variant_id=artifact["artifact_id"],
            profile_id=artifact["profile_id"],
            covered_targets=tuple(artifact["covered_clis"]),
        )
        for artifact in artifacts
    )
    component = PluginComponent(
        component_id=plugin_id,
        type_id=next(iter(types)),  # type: ignore[arg-type]
        variants=variants,
    )
    return LogicalPlugin(plugin_id=plugin_id, components=(component,))


__all__ = [
    "ComponentVariant",
    "LogicalPlugin",
    "PluginComponent",
    "decode_format_v1_plugin",
]
