"""Target-neutral logical Plugin/component domain tests."""

from pal.domain import (
    ComponentVariant,
    LogicalPlugin,
    PluginComponent,
    decode_format_v1_plugin,
)


def test_logical_plugin_domain_accepts_multiple_component_types() -> None:
    plugin = LogicalPlugin(
        "release-assistant",
        (
            PluginComponent(
                "instructions",
                "skill",
                (ComponentVariant("skill-shared", "skill-md-v1-basic", ("claude-code", "codex")),),
            ),
            PluginComponent(
                "repository-api",
                "mcp",
                (ComponentVariant("mcp-directed", "mcp-v1", ("claude-code", "codex")),),
            ),
            PluginComponent(
                "audit-hook",
                "hook",
                (ComponentVariant("hook-directed", "hook-v1", ("claude-code",)),),
            ),
        ),
    )

    assert [component.type_id for component in plugin.components] == ["skill", "mcp", "hook"]


def test_format_v1_codec_maps_one_skill_component_into_neutral_domain() -> None:
    plugin = decode_format_v1_plugin(
        "release-assistant",
        [
            {
                "artifact_id": "skill-shared",
                "kind": "skill",
                "profile_id": "skill-md-v1-basic",
                "covered_clis": ["claude-code", "codex"],
            }
        ],
    )

    assert plugin.plugin_id == "release-assistant"
    assert plugin.components[0].type_id == "skill"
