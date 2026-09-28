"""Validated, read-only Skill documents and payload browsing.

Traceability: PSM-009/010; PRD-RELEASE-002, PRD-MOUNT-004; ACC-011/012.
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

from .components.skill import parse_skill_document
from .errors import IntegrityError, PathSafetyError
from .io import sha256_bytes
from .library import doctor_library, load_json_object
from .paths import require_inside, require_safe_id
from .publishing import validate_development_revision, validate_production_version
from .schema_catalog import validate_instance
from .stages import current_production_id

MAX_PREVIEW_BYTES = 1024 * 1024


def render_markdown(text: str) -> str:
    """Render local prose without HTML execution or remote resource loading."""
    parser = MarkdownIt("commonmark", {"html": False}).enable("table")

    def link_open(tokens, index, _options, _env):
        href = tokens[index].attrGet("href") or ""
        parsed = urlsplit(href)
        if parsed.scheme in {"http", "https"}:
            return (
                f'<a href="{escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">'
            )
        # Relative links are resolved by the browser against the selected inventory only.
        return f'<a href="#" data-doc-link="{escape(href, quote=True)}">'

    parser.renderer.rules["link_open"] = link_open
    parser.renderer.rules["image"] = lambda tokens, index, _options, _env: (
        f'<span class="image-label">[图片：{escape(tokens[index].content)}]</span>'
    )
    return parser.render(text)


def _source(root: Path, unit_id: str, source: str) -> tuple[dict[str, Any], str, str | None]:
    require_safe_id(unit_id, "unit_id")
    context = doctor_library(root)
    if source in {"production", "mounted"}:
        version = (
            current_production_id(root)
            if source == "production"
            else context["active_production_version_id"]
        )
        if version:
            production = validate_production_version(root, version)
            for release in production["releases"]:
                if release["unit_id"] == unit_id:
                    return release, release["release"]["source_revision"]["revision_id"], version
        raise PathSafetyError("此 Skill 不在所选生产或挂载内容中，请重新打开详情")
    if source != "development":
        raise PathSafetyError("Skill 来源必须是 production 或 development")
    unit = load_json_object(require_inside(root, f"development/units/{unit_id}/unit.json", "Skill"))
    validate_instance("unit.schema.json", unit)
    revision = unit["current_revision_id"]
    return validate_development_revision(root, unit_id, revision), revision, None


def skill_artifacts(
    validated: dict[str, Any], *, include_files: bool = False
) -> list[dict[str, Any]]:
    documents = []
    for item in validated["artifacts"]:
        artifact = item["artifact"]
        canonical = f"skills/{artifact['unit_id']}/SKILL.md"
        path = require_inside(item["payload_root"], canonical, "Skill 说明")
        material = path.read_bytes()
        inventory = {entry["path"]: entry["sha256"] for entry in item["files"]}
        if sha256_bytes(material) != inventory.get(canonical):
            raise IntegrityError("Skill 说明在读取时发生变化")
        fields, body = parse_skill_document(material.decode("utf-8"))
        document = {
            "description": fields["description"],
            "instructions": body,
            "covered_clis": artifact["covered_clis"],
        }
        if include_files:
            prefix = f"skills/{artifact['unit_id']}/"
            common_root = prefix if all(name.startswith(prefix) for name in inventory) else ""
            document.update(
                {
                    "artifact_id": artifact["artifact_id"],
                    "profile_id": artifact["profile_id"],
                    "canonical_path": canonical,
                    "markdown_html": render_markdown(body),
                    "files": [
                        {
                            "path": name,
                            "display_path": name.removeprefix(common_root),
                            "sha256": digest,
                            "size": require_inside(item["payload_root"], name, "Skill 文件")
                            .stat()
                            .st_size,
                        }
                        for name, digest in sorted(inventory.items())
                    ],
                }
            )
        documents.append(document)
    return documents


def skill_details(root: Path, unit_id: str) -> dict[str, Any]:
    require_safe_id(unit_id, "unit_id")
    context = doctor_library(root)
    sources = {"development": None, "production": None, "mounted": None}
    path = require_inside(root, f"development/units/{unit_id}", "Skill 开发内容", must_exist=False)
    if path.exists():
        development, revision, _ = _source(root, unit_id, "development")
        sources["development"] = {
            "revision_id": revision,
            "version_id": None,
            "artifacts": skill_artifacts(development, include_files=True),
        }
    for source, version in (
        ("production", current_production_id(root)),
        ("mounted", context["active_production_version_id"]),
    ):
        if not version:
            continue
        validated = validate_production_version(root, version)
        for release in validated["releases"]:
            if release["unit_id"] == unit_id:
                sources[source] = {
                    "revision_id": release["release"]["source_revision"]["revision_id"],
                    "version_id": version,
                    "artifacts": skill_artifacts(release, include_files=True),
                }
    if not any(sources.values()):
        raise PathSafetyError("此 Skill 已不存在，请刷新列表")
    return {"unit_id": unit_id, **sources}


def skill_file(root: Path, parameters: dict[str, str]) -> dict[str, Any]:
    expected = {"unit_id", "source", "revision_id", "version_id", "artifact_id", "path"}
    if set(parameters) != expected:
        raise PathSafetyError("文件请求缺少版本、来源或文件标识")
    validated, revision, version = _source(root, parameters["unit_id"], parameters["source"])
    if parameters["revision_id"] != revision or parameters["version_id"] != (version or ""):
        raise IntegrityError("所选版本已变化，请返回集合重新打开详情")
    item = next(
        (
            item
            for item in validated["artifacts"]
            if item["artifact"]["artifact_id"] == parameters["artifact_id"]
        ),
        None,
    )
    if item is None:
        raise PathSafetyError("所选版本不包含此适配产物")
    entry = next((entry for entry in item["files"] if entry["path"] == parameters["path"]), None)
    if entry is None:
        raise PathSafetyError("文件不在此 Skill 产物清单中")
    path = require_inside(item["payload_root"], entry["path"], "Skill 文件")
    size = path.stat().st_size
    result = {"path": entry["path"], "size": size, "sha256": entry["sha256"]}
    if size > MAX_PREVIEW_BYTES:
        return {**result, "kind": "large", "message": "文件超过 1 MiB，暂不提供在线预览。"}
    with path.open("rb") as handle:
        material = handle.read(MAX_PREVIEW_BYTES + 1)
    if sha256_bytes(material) != entry["sha256"]:
        raise IntegrityError("文件在读取时发生变化，请重新打开详情")
    try:
        text = material.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None or any(ord(char) < 32 and char not in "\t\n\r" for char in text):
        return {
            **result,
            "kind": "binary",
            "message": "此文件为二进制或非 UTF-8 内容，暂不提供在线预览。",
        }
    markdown = path.suffix.lower() in {".md", ".markdown"}
    return {
        **result,
        "kind": "markdown" if markdown else "text",
        "content": text,
        "markdown_html": render_markdown(text) if markdown else None,
    }
