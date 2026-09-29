"""The registered ``pal.skill/v1`` Skill component contract.

Traceability: PRD-SPEC-002, PRD-CREATE-001/004, PRD-RELEASE-001;
ACC-003 through ACC-007, ACC-012. PRD sections 5.3 and 8.1 cover bundled files.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..errors import CreationError, IntegrityError
from ..io import sha256_bytes, sha256_file
from ..paths import normalize_relative_path, require_inside, validate_regular_tree
from ..platform_support import is_link
from .base import ComponentTypeDriver, PayloadFile, ValidatedCandidate

SKILL_COMPONENT_TYPE_ID = "skill"
SKILL_KIND_ID = SKILL_COMPONENT_TYPE_ID
SKILL_CONTRACT_ID = "pal.skill/v1"
SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SKILL_BYTES = 1024 * 1024


def parse_skill_document(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise CreationError("SKILL.md must begin with YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise CreationError("SKILL.md frontmatter is not closed") from exc
    fields: dict[str, str] = {}
    for raw_line in lines[1:end]:
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if ":" not in raw_line:
            raise CreationError(f"unsupported SKILL.md frontmatter line: {raw_line}")
        key, raw_value = raw_line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if not key or not value:
            raise CreationError("SKILL.md frontmatter keys and values must be non-empty")
        if key in fields:
            raise CreationError(f"duplicate SKILL.md frontmatter field: {key}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        fields[key] = value
    body = "\n".join(lines[end + 1 :]).strip()
    if not body:
        raise CreationError("SKILL.md instruction body must be non-empty")
    return fields, body


class SkillComponentDriver(ComponentTypeDriver):
    """Skill-specific semantics behind the generic PAL Plugin lifecycle core."""

    type_id = SKILL_COMPONENT_TYPE_ID
    contract_id = SKILL_CONTRACT_ID

    def accepts_unit_id(self, unit_id: str) -> bool:
        return len(unit_id) <= 64 and SKILL_NAME.fullmatch(unit_id) is not None

    def validate_profile(self, profile: dict[str, Any]) -> None:
        if profile.get("artifact_kind") != self.type_id:
            raise IntegrityError("Skill driver received a profile for another component type")
        if profile.get("canonical_path") != "skills/<skill-name>/SKILL.md":
            raise IntegrityError("Skill capability profile canonical path is unsupported")
        if profile.get("body_format") != "markdown":
            raise IntegrityError("Skill capability profile body format is unsupported")

    def plan_candidate(self, unit_id: str, artifact_id: str) -> dict[str, Any]:
        payload_root = f"generated/{artifact_id}/payload"
        return {
            "payload_root": payload_root,
            "skill_path": f"{payload_root}/skills/{unit_id}/SKILL.md",
        }

    def candidate_paths(self, artifact_plan: dict[str, Any]) -> tuple[str, ...]:
        path = artifact_plan.get("skill_path")
        if not isinstance(path, str):
            raise IntegrityError("Skill candidate plan has no skill_path")
        return (normalize_relative_path(path, "Skill candidate path"),)

    def candidate_tree_roots(self, artifact_plan: dict[str, Any]) -> tuple[str, ...]:
        (relative,) = self.candidate_paths(artifact_plan)
        return (Path(relative).parent.as_posix(),)

    def public_candidate_paths(
        self,
        transaction_root: Path,
        artifact_plan: dict[str, Any],
    ) -> dict[str, str]:
        (relative,) = self.candidate_paths(artifact_plan)
        return {
            "skill_path": str(transaction_root / relative),
            "skill_root": str((transaction_root / relative).parent),
        }

    def validate_candidate(
        self,
        transaction_root: Path,
        artifact_plan: dict[str, Any],
        unit_id: str,
        profile: dict[str, Any],
    ) -> ValidatedCandidate:
        self.validate_profile(profile)
        (relative,) = self.candidate_paths(artifact_plan)
        payload_relative = normalize_relative_path(
            artifact_plan.get("payload_root"), "Skill candidate payload root"
        )
        if relative != f"{payload_relative}/skills/{unit_id}/SKILL.md":
            raise IntegrityError("Skill candidate does not match its canonical payload path")
        payload_root = require_inside(transaction_root, payload_relative, "Skill candidate payload")
        validate_regular_tree(payload_root)
        path = require_inside(
            transaction_root,
            relative,
            f"candidate {artifact_plan['artifact_id']} SKILL.md",
        )
        if not path.is_file():
            raise CreationError(f"SKILL.md must be a regular file: {path}")
        skill_root = path.parent
        for entry in payload_root.rglob("*"):
            if entry.is_relative_to(skill_root):
                continue
            if entry.is_dir() and entry in skill_root.parents:
                continue
            raise CreationError(f"candidate file or directory is outside its Skill root: {entry}")
        material = path.read_bytes()
        if len(material) > MAX_SKILL_BYTES:
            raise CreationError(f"SKILL.md exceeds {MAX_SKILL_BYTES} bytes: {path}")
        try:
            text = material.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CreationError(f"SKILL.md is not UTF-8: {path}") from exc
        if text.startswith("\ufeff"):
            raise CreationError(f"SKILL.md must not contain a UTF-8 BOM: {path}")
        fields, _body = parse_skill_document(text)
        expected_fields = [field["name"] for field in profile["frontmatter_fields"]]
        if set(fields) != set(expected_fields):
            raise CreationError(
                f"SKILL.md fields for {profile['profile_id']} must be exactly {expected_fields}"
            )
        if fields["name"] != unit_id:
            raise CreationError("SKILL.md name must match the logical unit ID")
        name_constraints = profile["name_constraints"]
        if (
            len(fields["name"]) > name_constraints["max_length"]
            or re.fullmatch(name_constraints["pattern"], fields["name"]) is None
        ):
            raise CreationError("SKILL.md name violates the selected capability profile")
        description = fields["description"]
        description_constraints = profile["description_constraints"]
        if not description or len(description) > description_constraints["max_length"]:
            raise CreationError("SKILL.md description violates the selected capability profile")
        if description_constraints["forbid_angle_brackets"] and (
            "<" in description or ">" in description
        ):
            raise CreationError("SKILL.md description must not contain angle brackets")
        if "model" in fields and not fields["model"]:
            raise CreationError("SKILL.md model must be a non-empty string")

        files = []
        for entry in sorted(skill_root.rglob("*")):
            if not entry.is_file():
                continue
            file_relative = entry.relative_to(payload_root).as_posix()
            normalize_relative_path(file_relative, "Skill candidate payload file")
            content = material if entry == path else entry.read_bytes()
            files.append(
                PayloadFile(
                    path=file_relative,
                    sha256=sha256_bytes(content),
                    material=content,
                )
            )
        return ValidatedCandidate(files=tuple(files))

    def _canonical_skill(self, artifact: dict[str, Any], payload_root: Path) -> Path:
        if artifact.get("kind") != self.type_id:
            raise IntegrityError("Skill driver received an artifact for another kind")
        expected = payload_root / f"skills/{artifact['unit_id']}/SKILL.md"
        matches = sorted(payload_root.glob("skills/*/SKILL.md"))
        if matches != [expected] or is_link(expected) or not expected.is_file():
            raise IntegrityError(
                f"Skill artifact must contain one canonical SKILL.md: {artifact['artifact_id']}"
            )
        return expected

    def validate_payload(self, artifact: dict[str, Any], payload_root: Path) -> None:
        canonical = self._canonical_skill(artifact, payload_root)
        relative = canonical.relative_to(payload_root).as_posix()
        files = {entry["path"]: entry["sha256"] for entry in artifact["files"]}
        if files.get(relative) != sha256_file(canonical):
            raise IntegrityError(
                f"Skill artifact canonical file is absent from its inventory: "
                f"{artifact['artifact_id']}"
            )

    def projection_files(
        self,
        artifact: dict[str, Any],
        payload_root: Path,
    ) -> dict[str, bytes]:
        self._canonical_skill(artifact, payload_root)
        files: dict[str, bytes] = {}
        for path in sorted(payload_root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(payload_root).as_posix()
            normalize_relative_path(relative, "Skill production projection payload path")
            files[relative] = path.read_bytes()
        return files

    def runtime_candidates(
        self,
        *,
        artifact: dict[str, Any],
        production_artifact: dict[str, Any],
        unit_id: str,
        payload_root: Path,
        projected_root: Path,
        runtime_root: Path,
    ) -> list[dict[str, Any]]:
        if artifact.get("unit_id") != unit_id:
            raise IntegrityError("runtime Skill unit differs from its release")
        source_skill = self._canonical_skill(artifact, payload_root)
        relative = source_skill.relative_to(payload_root).as_posix()
        projected_skill = projected_root / relative
        runtime_skill = runtime_root / relative
        if is_link(projected_skill) or not projected_skill.is_file():
            raise IntegrityError(f"runtime projected Skill is unavailable: {projected_skill}")
        source_sha256 = sha256_file(source_skill)
        if sha256_file(projected_skill) != source_sha256:
            raise IntegrityError(
                f"runtime projected Skill drifted: {production_artifact['artifact_id']}"
            )
        if is_link(runtime_skill) or not runtime_skill.is_file():
            raise IntegrityError(f"runtime loaded Skill is unavailable: {runtime_skill}")
        if sha256_file(runtime_skill) != source_sha256:
            raise IntegrityError(
                f"runtime loaded Skill drifted: {production_artifact['artifact_id']}"
            )
        return [
            {
                "unit_id": unit_id,
                "release_id": production_artifact["release_id"],
                "artifact_id": production_artifact["artifact_id"],
                "profile_id": production_artifact["profile_id"],
                "covered_clis": production_artifact["covered_clis"],
                "tree_sha256": production_artifact["tree_sha256"],
                "relative_skill_path": relative,
                "skill_name": source_skill.parent.name,
                "skill_sha256": source_sha256,
                "projected_skill_path": str(projected_skill),
                "runtime_skill_path": str(runtime_skill),
            }
        ]

    def inspect_usage_events(
        self,
        cli_id: str,
        events: list[dict[str, Any]],
        context: dict[str, Any],
        *,
        verify_runtime: bool = True,
    ) -> Any:
        from ..usage_events import inspect_events

        return inspect_events(
            cli_id,
            events,
            context,
            verify_runtime=verify_runtime,
        )


__all__ = [
    "MAX_SKILL_BYTES",
    "SKILL_COMPONENT_TYPE_ID",
    "SKILL_CONTRACT_ID",
    "SKILL_KIND_ID",
    "SkillComponentDriver",
]
