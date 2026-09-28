"""Registered Plugin-component contract and Skill v1 driver tests.

Traceability: PRD-P0-003, PRD-SPEC-002, PRD-CREATE-002/003,
PRD-RELEASE-001, PRD-USE-001/002; ACC-001, ACC-005 through ACC-012.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from pal.components import (
    SKILL_COMPONENT_TYPE_ID,
    SKILL_CONTRACT_ID,
    ComponentTypeRegistry,
    SkillComponentDriver,
    component_type_driver,
    component_type_driver_for_profile,
    registered_component_contracts,
)
from pal.errors import CreationError, IntegrityError
from pal.io import sha256_bytes


def _basic_profile() -> dict[str, object]:
    return {
        "schema_version": 1,
        "profile_id": "skill-md-v1-basic",
        "artifact_kind": "skill",
        "extends": None,
        "covered_clis": ["claude-code", "codex"],
        "cli_versions": {"claude-code": "2.1.234", "codex": "0.147.0"},
        "canonical_path": "skills/<skill-name>/SKILL.md",
        "frontmatter_fields": [
            {"name": "name", "required": True, "value_type": "string"},
            {"name": "description", "required": True, "value_type": "string"},
        ],
        "name_constraints": {"pattern": "^[a-z0-9-]+$", "max_length": 64},
        "description_constraints": {
            "max_length": 1024,
            "forbid_angle_brackets": True,
        },
        "body_format": "markdown",
        "captured_at": "2026-08-18T11:50:00+08:00",
    }


def _skill(unit_id: str, description: str = "Perform a deterministic workflow.") -> bytes:
    return (
        "---\n"
        f"name: {unit_id}\n"
        f"description: {description}\n"
        "---\n\n"
        "# Instructions\n\nReturn the requested result.\n"
    ).encode()


def test_builtin_registry_exposes_only_the_skill_v1_contract() -> None:
    assert registered_component_contracts() == (
        {"type_id": SKILL_COMPONENT_TYPE_ID, "contract_id": SKILL_CONTRACT_ID},
    )
    assert component_type_driver(SKILL_COMPONENT_TYPE_ID) is component_type_driver_for_profile(
        _basic_profile()
    )


def test_registry_rejects_unknown_and_duplicate_kind_contracts() -> None:
    registry = ComponentTypeRegistry([SkillComponentDriver()])

    with pytest.raises(IntegrityError, match="not registered"):
        registry.require_type("mcp")
    with pytest.raises(IntegrityError, match="not registered"):
        registry.require_contract("pal.mcp/v1")
    with pytest.raises(ValueError, match="component type driver is already registered"):
        registry.register(SkillComponentDriver())

    duplicate_contract = SkillComponentDriver()
    duplicate_contract.type_id = "other"
    with pytest.raises(ValueError, match="contract is already registered"):
        registry.register(duplicate_contract)


def test_skill_driver_preserves_v1_candidate_and_payload_contract(tmp_path: Path) -> None:
    driver = SkillComponentDriver()
    profile = _basic_profile()
    unit_id = "example-skill"
    artifact_id = "artifact-example"
    plan = {"artifact_id": artifact_id, **driver.plan_candidate(unit_id, artifact_id)}

    assert plan["payload_root"] == f"generated/{artifact_id}/payload"
    assert plan["skill_path"] == (f"generated/{artifact_id}/payload/skills/{unit_id}/SKILL.md")
    assert driver.candidate_paths(plan) == (plan["skill_path"],)
    assert driver.candidate_tree_roots(plan) == (str(Path(plan["skill_path"]).parent),)
    assert driver.public_candidate_paths(tmp_path, plan) == {
        "skill_path": str(tmp_path / plan["skill_path"]),
        "skill_root": str((tmp_path / plan["skill_path"]).parent),
    }

    candidate_path = tmp_path / plan["skill_path"]
    candidate_path.parent.mkdir(parents=True)
    material = _skill(unit_id)
    candidate_path.write_bytes(material)
    validated = driver.validate_candidate(tmp_path, plan, unit_id, profile)

    assert len(validated.files) == 1
    assert validated.files[0].path == f"skills/{unit_id}/SKILL.md"
    assert validated.files[0].sha256 == sha256_bytes(material)
    assert validated.dependencies == ()

    payload_root = tmp_path / "committed-payload"
    payload_path = payload_root / validated.files[0].path
    payload_path.parent.mkdir(parents=True)
    payload_path.write_bytes(validated.files[0].material)
    artifact = {
        "artifact_id": artifact_id,
        "unit_id": unit_id,
        "kind": SKILL_COMPONENT_TYPE_ID,
        "files": [{"path": validated.files[0].path, "sha256": validated.files[0].sha256}],
    }

    driver.validate_payload(artifact, payload_root)
    assert driver.projection_files(artifact, payload_root) == {validated.files[0].path: material}


def test_skill_driver_collects_self_contained_text_and_binary_files(tmp_path: Path) -> None:
    driver = SkillComponentDriver()
    plan = {"artifact_id": "artifact-files", **driver.plan_candidate("files", "artifact-files")}
    canonical = tmp_path / plan["skill_path"]
    expected = {
        "SKILL.md": _skill("files"),
        "references/guide.txt": b"bundled reference\n",
        "scripts/read.py": b"print('bundled script')\n",
        "assets/template.bin": b"\x00\xff\x80",
    }
    for relative, material in expected.items():
        path = canonical.parent / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(material)
    validated = driver.validate_candidate(tmp_path, plan, "files", _basic_profile())
    assert {item.path: item.material for item in validated.files} == {
        f"skills/files/{relative}": material for relative, material in expected.items()
    }
    assert all(item.sha256 == sha256_bytes(item.material) for item in validated.files)
    assert validated.dependencies == ()


def test_skill_driver_rejects_a_second_skill_directory(tmp_path: Path) -> None:
    driver = SkillComponentDriver()
    plan = {"artifact_id": "artifact-files", **driver.plan_candidate("files", "artifact-files")}
    canonical = tmp_path / plan["skill_path"]
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(_skill("files"))
    sibling = canonical.parent.parent / "other" / "readme.txt"
    sibling.parent.mkdir()
    sibling.write_bytes(b"not part of this Skill")
    with pytest.raises(CreationError, match="outside its Skill root"):
        driver.validate_candidate(tmp_path, plan, "files", _basic_profile())


def test_skill_driver_fails_closed_on_profile_candidate_and_payload_drift(
    tmp_path: Path,
) -> None:
    driver = SkillComponentDriver()
    profile = _basic_profile()
    wrong_profile = deepcopy(profile)
    wrong_profile["canonical_path"] = "mcp/config.json"
    with pytest.raises(IntegrityError, match="canonical path"):
        driver.validate_profile(wrong_profile)

    unit_id = "invalid-skill"
    artifact_id = "artifact-invalid"
    plan = {"artifact_id": artifact_id, **driver.plan_candidate(unit_id, artifact_id)}
    candidate_path = tmp_path / plan["skill_path"]
    candidate_path.parent.mkdir(parents=True)
    candidate_path.write_bytes(_skill(unit_id, "Contains <unsupported> markup."))
    with pytest.raises(CreationError, match="angle brackets"):
        driver.validate_candidate(tmp_path, plan, unit_id, profile)

    payload_root = tmp_path / "payload"
    canonical = payload_root / f"skills/{unit_id}/SKILL.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(_skill(unit_id))
    extra = payload_root / "skills/other/SKILL.md"
    extra.parent.mkdir(parents=True)
    extra.write_bytes(_skill("other"))
    artifact = {
        "artifact_id": artifact_id,
        "unit_id": unit_id,
        "kind": SKILL_COMPONENT_TYPE_ID,
        "files": [
            {
                "path": f"skills/{unit_id}/SKILL.md",
                "sha256": sha256_bytes(canonical.read_bytes()),
            },
            {
                "path": "skills/other/SKILL.md",
                "sha256": sha256_bytes(extra.read_bytes()),
            },
        ],
    }
    with pytest.raises(IntegrityError, match="one canonical SKILL.md"):
        driver.validate_payload(artifact, payload_root)
