"""P1 Skill creation transactions and development-library commits.

The initiating LLM writes ``SKILL.md`` and supporting files only into candidate
directories that PAL pre-allocates. PAL owns profile branching, coverage validation, schema
materialization, and the single development commit point.

Traceability: PRD-CLI-001 through PRD-CLI-003, PRD-SPEC-001/002,
PRD-CREATE-001 through PRD-CREATE-005; ACC-003 through ACC-006, ACC-012.
"""

from __future__ import annotations

import os
import secrets
import shlex
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import locking as fcntl
from .components import (
    SKILL_COMPONENT_TYPE_ID,
    PayloadFile,
    ValidatedCandidate,
    component_type_driver,
    component_type_driver_for_profile,
)
from .config_mount import resolve_creation_context, resolve_default_creation_library
from .domain import decode_format_v1_plugin
from .errors import CreationError, IntegrityError, PathSafetyError, SchemaValidationError
from .io import (
    atomic_replace_json,
    canonical_json_bytes,
    formatted_json_bytes,
    fsync_directory,
    fsync_tree,
    sha256_bytes,
    sha256_file,
    tree_digest,
    write_new_bytes,
    write_new_json,
)
from .library import doctor_library, load_json_object, utc_now
from .maintenance import lifecycle_write
from .paths import (
    canonical_existing_root,
    normalize_relative_path,
    require_inside,
    require_safe_id,
    validate_regular_tree,
)
from .platform_support import is_link, move_path
from .schema_catalog import TARGET_CLIS, validate_config_instance, validate_instance

REQUEST_FIELDS = {"schema_version", "unit_id", "summary", "profile_by_cli"}
TRANSACTION_FILENAME = "transaction.json"
PLAN_FILENAME = "plan.json"
REQUEST_FILENAME = "request.json"
MAX_REQUEST_SUMMARY = 4096


def resolve_library_root(
    explicit: Path | None,
    *,
    cwd: Path | None = None,
    config_root: Path | None = None,
) -> Path:
    """Resolve a library by explicit, environment, cwd, then default binding."""

    if explicit is not None:
        return canonical_existing_root(explicit)
    configured = os.environ.get("PAL_LIBRARY_ROOT")
    if configured:
        return canonical_existing_root(Path(configured))

    start = canonical_existing_root(cwd or Path.cwd())
    for candidate in (start, *start.parents):
        manifest = candidate / "library.json"
        if is_link(manifest):
            raise PathSafetyError(f"discovered library manifest is a symbolic link: {manifest}")
        if manifest.is_file():
            return canonical_existing_root(candidate)
    default_library = resolve_default_creation_library(config_root=config_root)
    if default_library is not None:
        return default_library
    raise PathSafetyError(
        "no PAL library found from the current working directory and no default creation "
        "library is configured"
    )


def _creation_id() -> str:
    return f"creation-{secrets.token_hex(16)}"


def _revision_id(creation_id: str) -> str:
    if not creation_id.startswith("creation-"):
        raise CreationError(f"invalid PAL creation identity: {creation_id}")
    return f"revision-{creation_id.removeprefix('creation-')}"


def _load_request(path: Path) -> dict[str, Any]:
    absolute = Path(os.path.abspath(path))
    if is_link(absolute) or not absolute.is_file():
        raise PathSafetyError(f"creation request must be a regular file: {absolute}")
    request = load_json_object(absolute)
    fields = REQUEST_FIELDS | (
        {"base_revision_id"} if request.get("schema_version") == 2 else set()
    )
    if set(request) != fields:
        raise SchemaValidationError(
            "creation request fields must be exactly schema_version, unit_id, summary, "
            "and profile_by_cli"
        )
    if type(request["schema_version"]) is not int or request["schema_version"] not in (1, 2):
        raise SchemaValidationError("creation request schema_version must be 1 or 2")
    if request["schema_version"] == 2:
        require_safe_id(request["base_revision_id"], "base_revision_id")
    unit_id = require_safe_id(request["unit_id"], "unit_id")
    if not component_type_driver(SKILL_COMPONENT_TYPE_ID).accepts_unit_id(unit_id):
        raise SchemaValidationError(
            "unit_id must be a lower-case hyphen-delimited Skill name of at most 64 characters"
        )
    summary = request["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise SchemaValidationError("creation request summary must be a non-empty string")
    if len(summary) > MAX_REQUEST_SUMMARY:
        raise SchemaValidationError(
            f"creation request summary exceeds {MAX_REQUEST_SUMMARY} characters"
        )
    profiles = request["profile_by_cli"]
    if not isinstance(profiles, dict) or set(profiles) != set(TARGET_CLIS):
        raise SchemaValidationError("profile_by_cli must contain exactly claude-code and codex")
    for cli_id in TARGET_CLIS:
        require_safe_id(profiles[cli_id], f"profile_by_cli.{cli_id}")
    request["profile_by_cli"] = {cli_id: profiles[cli_id] for cli_id in TARGET_CLIS}
    return request


def _profile_references(root: Path) -> dict[str, dict[str, Any]]:
    manifest = load_json_object(require_inside(root, "library.json", "library manifest"))
    return {reference["profile_id"]: reference for reference in manifest["capability_profiles"]}


def _validate_requested_profiles(
    root: Path,
    request: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    available = _profile_references(root)
    selected: dict[str, dict[str, Any]] = {}
    for cli_id in TARGET_CLIS:
        profile_id = request["profile_by_cli"][cli_id]
        reference = available.get(profile_id)
        if reference is None:
            raise CreationError(f"requested profile is not validated by this library: {profile_id}")
        if cli_id not in reference["covered_clis"]:
            raise CreationError(f"profile {profile_id} does not cover target CLI {cli_id}")
        selected[profile_id] = reference
    return selected


def _load_profile_document(path: Path, expected_sha256: str, label: str) -> dict[str, Any]:
    if is_link(path) or not path.is_file():
        raise IntegrityError(f"{label} is unavailable: {path}")
    if sha256_file(path) != expected_sha256:
        raise IntegrityError(f"{label} SHA-256 mismatch")
    profile = load_json_object(path)
    validate_instance("capability-profile.schema.json", profile)
    component_type_driver_for_profile(profile)
    return profile


def _selected_profile_documents(
    root: Path,
    references: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        profile_id: _load_profile_document(
            require_inside(root, reference["path"], f"profile {profile_id}"),
            reference["sha256"],
            f"capability profile {profile_id}",
        )
        for profile_id, reference in references.items()
    }


def _plan_profile_documents(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    for reference in plan["context"]["profile_refs"]:
        profile_id = reference["profile_id"]
        documents[profile_id] = _load_profile_document(
            Path(reference["path"]),
            reference["sha256"],
            f"capability profile {profile_id}",
        )
    return documents


def _candidate_path_below_generated(value: object) -> str:
    relative = normalize_relative_path(value, "artifact candidate path")
    try:
        generated_relative = Path(relative).relative_to("generated")
    except ValueError as exc:
        raise CreationError("artifact driver candidate path must be below generated/") from exc
    if generated_relative == Path("."):
        raise CreationError("artifact driver candidate path must name a file below generated/")
    return relative


def _context_snapshot(
    root: Path,
    context: dict[str, Any],
    selected_profiles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    profile_refs: list[dict[str, Any]] = []
    for profile_id in selected_profiles:
        reference = selected_profiles[profile_id]
        path = require_inside(root, reference["path"], f"profile {profile_id}")
        profile_refs.append(
            {
                "profile_id": profile_id,
                "covered_clis": reference["covered_clis"],
                "cli_versions": reference["cli_versions"],
                "path": str(path),
                "sha256": reference["sha256"],
            }
        )
    return {
        "library_root": context["library_root"],
        "development_root": context["development_root"],
        "production_root": context["production_root"],
        "library_manifest_sha256": context["library_manifest_sha256"],
        "target_clis": context["target_clis"],
        "references": context["references"],
        "profile_refs": profile_refs,
    }


def _artifact_id(
    creation_id: str,
    profile_id: str,
    covered_clis: list[str],
) -> str:
    material = canonical_json_bytes(
        {
            "creation_id": creation_id,
            "profile_id": profile_id,
            "covered_clis": covered_clis,
        }
    )
    digest = sha256_bytes(material)[:12]
    scope = "shared" if covered_clis == TARGET_CLIS else covered_clis[0]
    return f"artifact-{scope}-{digest}"


def _build_plan(
    root: Path,
    request: dict[str, Any],
    context: dict[str, Any],
    creation_id: str,
    created_by_cli: str,
) -> dict[str, Any]:
    selected_profiles = _validate_requested_profiles(root, request)
    profile_documents = _selected_profile_documents(root, selected_profiles)
    groups: dict[str, list[str]] = {}
    for cli_id in TARGET_CLIS:
        groups.setdefault(request["profile_by_cli"][cli_id], []).append(cli_id)

    artifacts: list[dict[str, Any]] = []
    kind_ids: set[str] = set()
    for profile_id, covered_clis in groups.items():
        driver = component_type_driver_for_profile(profile_documents[profile_id])
        kind_ids.add(driver.type_id)
        artifact_id = _artifact_id(creation_id, profile_id, covered_clis)
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "profile_id": profile_id,
                "covered_clis": covered_clis,
                **driver.plan_candidate(request["unit_id"], artifact_id),
            }
        )
    if len(kind_ids) != 1:
        raise CreationError("one format-v1 creation must resolve to one component type")

    return {
        "schema_version": 1,
        "creation_id": creation_id,
        "revision_id": _revision_id(creation_id),
        "unit_id": request["unit_id"],
        "request_summary": request["summary"],
        "created_by_cli": created_by_cli,
        "target_clis": TARGET_CLIS.copy(),
        "profile_by_cli": request["profile_by_cli"],
        "context": _context_snapshot(root, context, selected_profiles),
        "artifacts": artifacts,
    }


def _transaction_parent(root: Path) -> Path:
    return require_inside(
        root,
        ".pal/transactions/creation",
        "creation transaction parent",
    )


def _transaction_root(root: Path, creation_id: str) -> Path:
    creation_id = require_safe_id(creation_id, "creation_id")
    return require_inside(
        root,
        f".pal/transactions/creation/{creation_id}",
        "creation transaction",
    )


def _transaction_record(transaction_root: Path) -> tuple[Path, dict[str, Any]]:
    path = transaction_root / TRANSACTION_FILENAME
    if is_link(path) or not path.is_file():
        raise PathSafetyError(f"creation transaction record is unavailable: {path}")
    transaction = load_json_object(path)
    validate_instance("creation-transaction.schema.json", transaction)
    return path, transaction


def _safe_remove_temporary(path: Path, parent: Path) -> None:
    try:
        path.relative_to(parent)
    except ValueError as exc:  # pragma: no cover - defensive invariant
        raise CreationError(f"refusing to clean temporary creation path: {path}") from exc
    if is_link(path) or not path.name.startswith(".pal-create-"):
        raise CreationError(f"refusing to clean unexpected creation path: {path}")
    shutil.rmtree(path)


def inspect_creation(
    library_root: Path, unit_id: str, *, config_root: Path | None = None
) -> dict[str, Any]:
    """Return a validated base revision for native CLI updates (PSM-003)."""
    from .publishing import validate_development_revision

    root = canonical_existing_root(library_root)
    context = resolve_creation_context(
        root, config_root=config_root, require_writable_development=False
    )
    unit_id = require_safe_id(unit_id, "unit_id")
    unit_path = require_inside(
        root, f"development/units/{unit_id}/unit.json", "unit manifest", must_exist=False
    )
    if not unit_path.is_file():
        unit_root = require_inside(root, "development/units", "development units root")
        candidates = [
            candidate.name
            for candidate in sorted(unit_root.iterdir(), key=lambda item: item.name.encode("utf-8"))
            if candidate.is_dir() and not is_link(candidate) and (candidate / "unit.json").is_file()
        ]
        if unit_id == context["library_id"]:
            prefix = f"{unit_id} 是外挂库 ID，不是 Skill 的 unit_id。"
        else:
            prefix = f"找不到 Skill unit_id：{unit_id}。"
        available = "、".join(candidates) if candidates else "（当前没有已提交的 Skill）"
        command = ["pal", "create", "list", "--library", str(root)]
        if config_root is not None:
            command.extend(["--config-root", str(config_root)])
        raise CreationError(
            f"{prefix}可选 Skill：{available}。"
            f"请先执行 `{shlex.join(command)}`，确认目标 Skill 和具体修改内容后再更新；"
            "此检查没有创建或修改任何事务。"
        )
    unit = load_json_object(unit_path)
    validate_instance("unit.schema.json", unit)
    base = validate_development_revision(
        root,
        unit_id,
        unit["current_revision_id"],
        require_writable_development=False,
    )
    return {
        "unit_id": unit_id,
        "revision_id": base["revision_id"],
        "profile_by_cli": {
            cli: item["artifact"]["profile_id"]
            for item in base["artifacts"]
            for cli in item["artifact"]["covered_clis"]
        },
        "artifacts": [
            {
                "profile_id": item["artifact"]["profile_id"],
                "covered_clis": item["artifact"]["covered_clis"],
                "skill_root": str(item["payload_root"] / "skills" / unit_id),
            }
            for item in base["artifacts"]
        ],
    }


def list_creation_units(library_root: Path, *, config_root: Path | None = None) -> dict[str, Any]:
    """List committed Skills and their production state for update selection."""
    from .publishing import validate_production_version

    root = canonical_existing_root(library_root)
    context = resolve_creation_context(
        root, config_root=config_root, require_writable_development=False
    )
    unit_root = require_inside(root, "development/units", "development units root")
    active_by_unit: dict[str, str] = {}
    # Publication is independent of CLI selection: an unmounted Skill stays published.
    doctor = doctor_library(root, require_writable_development=False)
    active_version = doctor["published_production_version_id"]
    if active_version is not None:
        active = validate_production_version(root, active_version)
        for release in active["releases"]:
            active_by_unit[release["unit_id"]] = release["release"]["source_revision"][
                "revision_id"
            ]

    units: list[dict[str, Any]] = []
    for candidate in sorted(unit_root.iterdir(), key=lambda item: item.name.encode("utf-8")):
        if is_link(candidate) or not candidate.is_dir():
            raise PathSafetyError(f"development unit entry is invalid: {candidate}")
        unit_id = require_safe_id(candidate.name, "development unit directory")
        unit = load_json_object(candidate / "unit.json")
        validate_instance("unit.schema.json", unit)
        if unit["unit_id"] != unit_id:
            raise IntegrityError(
                f"development unit identity differs from its directory: {candidate}"
            )
        current_revision = unit["current_revision_id"]
        active_revision = active_by_unit.get(unit_id)
        units.append(
            {
                "unit_id": unit_id,
                "current_revision_id": current_revision,
                "active_revision_id": active_revision,
                "state": (
                    "unpublished"
                    if active_revision is None
                    else "published"
                    if active_revision == current_revision
                    else "changed"
                ),
            }
        )
    return {"library_id": context["library_id"], "units": units}


@lifecycle_write
def begin_creation(
    library_root: Path,
    cli_id: str,
    request_file: Path,
    *,
    config_root: Path | None = None,
) -> dict[str, Any]:
    """Open a P1 transaction and return PAL-controlled candidate output paths."""

    if cli_id not in TARGET_CLIS:
        raise CreationError(f"unsupported creation CLI: {cli_id}")
    root = canonical_existing_root(library_root)
    context = resolve_creation_context(root, config_root=config_root)
    request = _load_request(request_file)
    from .skill_state import require_live_skill

    require_live_skill(root, request["unit_id"])
    unit_target = require_inside(
        root,
        f"development/units/{request['unit_id']}",
        "target logical unit",
        must_exist=False,
    )
    base = None
    if request.get("base_revision_id"):
        from .publishing import validate_development_revision

        unit_path = require_inside(unit_target, "unit.json", "base unit manifest")
        unit = load_json_object(unit_path)
        validate_instance("unit.schema.json", unit)
        if unit["current_revision_id"] != request["base_revision_id"]:
            raise CreationError("Skill 开发版本已变化，请基于当前 revision 重新开始更新")
        base = validate_development_revision(root, request["unit_id"], request["base_revision_id"])
    elif unit_target.exists() or is_link(unit_target):
        raise CreationError(
            "logical unit already exists; initial P1 does not patch existing units: "
            f"{request['unit_id']}"
        )

    creation_id = _creation_id()
    plan = _build_plan(root, request, context, creation_id, cli_id)
    parent = _transaction_parent(root)
    final = parent / creation_id
    temporary = parent / f".pal-create-{creation_id}.tmp"
    if final.exists() or temporary.exists():  # practically impossible, still fail closed
        raise CreationError(f"creation identity already exists: {creation_id}")

    created_at = utc_now()
    request_material = formatted_json_bytes(request)
    transaction = {
        "schema_version": 1,
        "creation_id": creation_id,
        "library_id": context["library_id"],
        "created_by_cli": cli_id,
        "state": "OPEN",
        "staging_root": str(final),
        "request": {
            "path": REQUEST_FILENAME,
            "sha256": sha256_bytes(request_material),
        },
        "target_clis": TARGET_CLIS.copy(),
        "profile_ids": list(dict.fromkeys(request["profile_by_cli"].values())),
        "created_at": created_at,
        "updated_at": created_at,
        "error": None,
    }
    validate_instance("creation-transaction.schema.json", transaction)

    committed = False
    try:
        temporary.mkdir()
        write_new_bytes(temporary / REQUEST_FILENAME, request_material)
        write_new_json(temporary / PLAN_FILENAME, plan)
        profile_documents = _plan_profile_documents(plan)
        for artifact in plan["artifacts"]:
            driver = component_type_driver_for_profile(profile_documents[artifact["profile_id"]])
            for relative in driver.candidate_paths(artifact):
                relative = _candidate_path_below_generated(relative)
                (temporary / relative).parent.mkdir(parents=True, exist_ok=True)
            if base is not None:
                # Preserve the complete bundle. If regrouping two directed
                # variants into one shared candidate, require explicit authoring.
                sources = [
                    item
                    for item in base["artifacts"]
                    if set(artifact["covered_clis"]).issubset(item["artifact"]["covered_clis"])
                ]
                if len(sources) == 1:
                    source = sources[0]["payload_root"] / "skills" / request["unit_id"]
                    destination = Path(
                        driver.public_candidate_paths(temporary, artifact)["skill_root"]
                    )
                    shutil.copytree(source, destination, dirs_exist_ok=True)
        write_new_json(temporary / TRANSACTION_FILENAME, transaction)
        fsync_tree(temporary)
        move_path(temporary, final)
        fsync_directory(parent)
        committed = True
    finally:
        if not committed and temporary.exists():
            _safe_remove_temporary(temporary, parent)

    return {
        "creation_id": creation_id,
        "library_id": context["library_id"],
        "unit_id": request["unit_id"],
        "revision_id": plan["revision_id"],
        "transaction_path": str(final / TRANSACTION_FILENAME),
        "target_clis": TARGET_CLIS.copy(),
        "context": {
            "references": context["references"],
            "profile_ids": context["profile_ids"],
        },
        "candidates": [
            {
                "artifact_id": artifact["artifact_id"],
                "profile_id": artifact["profile_id"],
                "covered_clis": artifact["covered_clis"],
                **component_type_driver_for_profile(
                    profile_documents[artifact["profile_id"]]
                ).public_candidate_paths(final, artifact),
            }
            for artifact in plan["artifacts"]
        ],
    }


def _update_transaction(
    path: Path,
    transaction: dict[str, Any],
    state: str,
    *,
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    updated = {**transaction, "state": state, "updated_at": utc_now(), "error": error}
    validate_instance("creation-transaction.schema.json", updated)
    atomic_replace_json(path, updated)
    return updated


def _abort_after_failure(path: Path, message: str) -> None:
    try:
        transaction = load_json_object(path)
        validate_instance("creation-transaction.schema.json", transaction)
        if transaction["state"] not in {"COMMITTED", "ABORTED"}:
            _update_transaction(
                path,
                transaction,
                "ABORTED",
                error={"code": "creation-failed", "message": message or "creation failed"},
            )
    except (OSError, IntegrityError, PathSafetyError, SchemaValidationError):
        # Preserve the original rejection. A malformed/unwritable transaction
        # is itself evidence that recovery must not guess a successful state.
        return


def _validate_plan_and_request(
    root: Path,
    transaction_root: Path,
    transaction: dict[str, Any],
    context: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    request_path = require_inside(
        transaction_root,
        transaction["request"]["path"],
        "creation request snapshot",
    )
    if not request_path.is_file():
        raise IntegrityError(f"creation request snapshot is not a file: {request_path}")
    if sha256_file(request_path) != transaction["request"]["sha256"]:
        raise IntegrityError("creation request snapshot SHA-256 mismatch")
    request = _load_request(request_path)

    plan_path = require_inside(transaction_root, PLAN_FILENAME, "creation plan")
    plan = load_json_object(plan_path)
    expected = _build_plan(
        root,
        request,
        context,
        transaction["creation_id"],
        transaction["created_by_cli"],
    )
    if plan != expected:
        raise IntegrityError("creation plan or mounted context has drifted")
    if transaction["profile_ids"] != list(dict.fromkeys(request["profile_by_cli"].values())):
        raise IntegrityError("creation transaction profile set does not match its request")
    return request, plan


def _expected_candidate_directories(
    plan: dict[str, Any],
    profile_documents: dict[str, dict[str, Any]],
) -> set[str]:
    expected: set[str] = set()
    for artifact in plan["artifacts"]:
        driver = component_type_driver_for_profile(profile_documents[artifact["profile_id"]])
        for candidate_path in driver.candidate_paths(artifact):
            relative = Path(_candidate_path_below_generated(candidate_path)).relative_to(
                "generated"
            )
            parent = relative.parent
            while parent != Path("."):
                expected.add(parent.as_posix())
                parent = parent.parent
    return expected


def _validate_candidate_layout(
    transaction_root: Path,
    plan: dict[str, Any],
) -> list[dict[str, Any]]:
    generated = require_inside(transaction_root, "generated", "generated candidate root")
    if not generated.is_dir():
        raise CreationError("generated candidate root is not a directory")
    validate_regular_tree(generated)
    profile_documents = _plan_profile_documents(plan)
    expected_files: set[str] = set()
    tree_roots: list[Path] = []
    for artifact in plan["artifacts"]:
        driver = component_type_driver_for_profile(profile_documents[artifact["profile_id"]])
        expected_files.update(
            Path(_candidate_path_below_generated(path)).relative_to("generated").as_posix()
            for path in driver.candidate_paths(artifact)
        )
        tree_roots.extend(
            Path(_candidate_path_below_generated(path)).relative_to("generated")
            for path in driver.candidate_tree_roots(artifact)
        )
    actual_files = {
        path.relative_to(generated).as_posix() for path in generated.rglob("*") if path.is_file()
    }
    unexpected_files = {
        path
        for path in actual_files - expected_files
        if not any(Path(path).is_relative_to(root) for root in tree_roots)
    }
    if not expected_files.issubset(actual_files) or unexpected_files:
        raise CreationError(
            f"candidate file inventory mismatch: missing {sorted(expected_files - actual_files)}, "
            f"outside candidate roots {sorted(unexpected_files)}"
        )
    actual_directories = {
        path.relative_to(generated).as_posix() for path in generated.rglob("*") if path.is_dir()
    }
    expected_directories = _expected_candidate_directories(plan, profile_documents)
    unexpected_directories = {
        path
        for path in actual_directories - expected_directories
        if not any(Path(path).is_relative_to(root) for root in tree_roots)
    }
    if not expected_directories.issubset(actual_directories) or unexpected_directories:
        raise CreationError("candidate directory inventory does not match the PAL output contract")

    coverage = [cli for artifact in plan["artifacts"] for cli in artifact["covered_clis"]]
    if coverage != TARGET_CLIS:
        raise CreationError("artifact coverage must be disjoint and complete in target CLI order")
    return plan["artifacts"]


def _validate_candidate_protocols(
    transaction_root: Path,
    plan: dict[str, Any],
) -> list[dict[str, Any]]:
    profile_documents = _plan_profile_documents(plan)

    validated: list[dict[str, Any]] = []
    for artifact in plan["artifacts"]:
        profile = profile_documents.get(artifact["profile_id"])
        if profile is None:
            raise CreationError(
                f"artifact references an unavailable profile: {artifact['profile_id']}"
            )
        if any(cli_id not in profile["covered_clis"] for cli_id in artifact["covered_clis"]):
            raise CreationError("artifact coverage exceeds its selected capability profile")
        driver = component_type_driver_for_profile(profile)
        result = driver.validate_candidate(
            transaction_root,
            artifact,
            plan["unit_id"],
            profile,
        )
        validated.append(
            {
                **artifact,
                "artifact_kind": driver.type_id,
                "validated_candidate": result,
            }
        )
    return validated


def _copy_context_snapshots(
    revision_root: Path,
    plan: dict[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    specification_refs: list[dict[str, str]] = []
    references = plan["context"]["references"]
    categories: list[tuple[str, list[dict[str, str]]]] = [
        ("common", references["common_specifications"]),
        ("cli/claude-code", references["cli_specifications"]["claude-code"]),
        ("cli/codex", references["cli_specifications"]["codex"]),
        ("design-rules", references["design_rules"]),
    ]
    for category, entries in categories:
        for index, reference in enumerate(entries):
            source = Path(reference["path"])
            if is_link(source) or sha256_file(source) != reference["sha256"]:
                raise IntegrityError(f"creation context reference drifted: {source}")
            relative = f"context/specifications/{category}/{index:03d}-{source.name}"
            target = revision_root / relative
            write_new_bytes(target, source.read_bytes())
            specification_refs.append({"path": relative, "sha256": reference["sha256"]})

    profile_refs: list[dict[str, str]] = []
    for reference in plan["context"]["profile_refs"]:
        source = Path(reference["path"])
        if is_link(source) or sha256_file(source) != reference["sha256"]:
            raise IntegrityError(f"creation profile reference drifted: {source}")
        relative = f"context/profiles/{reference['profile_id']}.json"
        write_new_bytes(revision_root / relative, source.read_bytes())
        profile_refs.append({"path": relative, "sha256": reference["sha256"]})
    return specification_refs, profile_refs


def _prepare_unit(
    transaction_root: Path,
    request: dict[str, Any],
    plan: dict[str, Any],
    transaction: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> Path:
    prepared_parent = transaction_root / "prepared"
    if prepared_parent.exists():
        if is_link(prepared_parent) or not prepared_parent.is_dir():
            raise PathSafetyError(f"invalid prepared unit root: {prepared_parent}")
        validate_regular_tree(prepared_parent)
        shutil.rmtree(prepared_parent)
    prepared_unit = prepared_parent / plan["unit_id"]
    revision_root = prepared_unit / "revisions" / plan["revision_id"]
    revision_root.mkdir(parents=True)

    source_relative = "source/request.json"
    source_material = (transaction_root / REQUEST_FILENAME).read_bytes()
    write_new_bytes(revision_root / source_relative, source_material)
    specification_refs, profile_refs = _copy_context_snapshots(revision_root, plan)

    artifact_refs: list[dict[str, Any]] = []
    for candidate in candidates:
        driver = component_type_driver(candidate["artifact_kind"])
        validated_candidate = candidate["validated_candidate"]
        if not isinstance(validated_candidate, ValidatedCandidate):
            raise CreationError("artifact driver returned an invalid candidate value")
        if not validated_candidate.files:
            raise CreationError("artifact driver returned an empty payload")
        artifact_root = revision_root / "artifacts" / candidate["artifact_id"]
        payload_root = artifact_root / "payload"
        payload_paths: set[str] = set()
        file_entries: list[dict[str, str]] = []
        for payload_file in validated_candidate.files:
            if not isinstance(payload_file, PayloadFile):
                raise CreationError("artifact driver returned an invalid payload file")
            relative = normalize_relative_path(
                payload_file.path,
                f"artifact {candidate['artifact_id']} payload file",
            )
            if relative in payload_paths:
                raise CreationError(f"artifact driver returned a duplicate file: {relative}")
            if sha256_bytes(payload_file.material) != payload_file.sha256:
                raise CreationError(f"artifact driver returned a wrong digest: {relative}")
            payload_paths.add(relative)
            write_new_bytes(payload_root / relative, payload_file.material)
            file_entries.append({"path": relative, "sha256": payload_file.sha256})
        artifact = {
            "schema_version": 1,
            "artifact_id": candidate["artifact_id"],
            "unit_id": plan["unit_id"],
            "revision_id": plan["revision_id"],
            "kind": driver.type_id,
            "profile_id": candidate["profile_id"],
            "covered_clis": candidate["covered_clis"],
            "payload_root": "payload",
            "files": file_entries,
            "tree_sha256": tree_digest(payload_root),
            "dependencies": list(validated_candidate.dependencies),
        }
        validate_instance("artifact.schema.json", artifact)
        files_by_path = {entry["path"]: entry["sha256"] for entry in file_entries}
        for dependency in artifact["dependencies"]:
            if files_by_path.get(dependency["path"]) != dependency["sha256"]:
                raise CreationError(
                    f"artifact driver returned an unclosed dependency: "
                    f"{dependency['dependency_id']}"
                )
        driver.validate_payload(artifact, payload_root)
        manifest_path = artifact_root / "artifact.json"
        write_new_json(manifest_path, artifact)
        manifest_relative = manifest_path.relative_to(revision_root).as_posix()
        artifact_refs.append(
            {
                "artifact_id": artifact["artifact_id"],
                "manifest": {
                    "path": manifest_relative,
                    "sha256": sha256_file(manifest_path),
                },
            }
        )

    try:
        logical_plugin = decode_format_v1_plugin(
            plan["unit_id"],
            [
                {
                    "artifact_id": candidate["artifact_id"],
                    "profile_id": candidate["profile_id"],
                    "covered_clis": candidate["covered_clis"],
                    "kind": candidate["artifact_kind"],
                }
                for candidate in candidates
            ],
        )
    except IntegrityError as exc:
        raise CreationError(str(exc)) from exc
    unit_kind = logical_plugin.components[0].type_id

    created_at = utc_now()
    revision = {
        "schema_version": 1,
        "revision_id": plan["revision_id"],
        "creation_id": transaction["creation_id"],
        "unit_id": plan["unit_id"],
        "request": {
            "summary": request["summary"],
            "sha256": sha256_bytes(source_material),
        },
        "source_files": [{"path": source_relative, "sha256": sha256_bytes(source_material)}],
        "artifacts": artifact_refs,
        "created_by_cli": transaction["created_by_cli"],
        "target_clis": TARGET_CLIS.copy(),
        "specification_refs": specification_refs,
        "profile_refs": profile_refs,
        "created_at": created_at,
    }
    validate_instance("development-revision.schema.json", revision)
    write_new_json(revision_root / "revision.json", revision)

    unit = {
        "schema_version": 1,
        "unit_id": plan["unit_id"],
        "kind": unit_kind,
        "current_revision_id": plan["revision_id"],
        "revision_ids": [plan["revision_id"]],
        "created_at": created_at,
        "updated_at": created_at,
    }
    validate_instance("unit.schema.json", unit)
    write_new_json(prepared_unit / "unit.json", unit)
    fsync_tree(prepared_unit)
    return prepared_unit


@contextmanager
def _unit_lock(root: Path, unit_id: str) -> Iterator[None]:
    if fcntl is None:  # pragma: no cover - doctor already rejects this platform
        raise CreationError("P1 creation requires a supported file lock backend")
    lock_parent = require_inside(root, ".pal/locks", "PAL lock root")
    lock_path = lock_parent / f"unit-{unit_id}.lock"
    if is_link(lock_path):
        raise PathSafetyError(f"unit lock cannot be a symbolic link: {lock_path}")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CreationError(f"logical unit is locked by another creation: {unit_id}") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _committed_result(
    root: Path,
    transaction: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    unit_root = require_inside(
        root,
        f"development/units/{plan['unit_id']}",
        "committed logical unit",
    )
    unit = load_json_object(unit_root / "unit.json")
    validate_instance("unit.schema.json", unit)
    unit_driver = component_type_driver(unit["kind"])
    if unit["unit_id"] != plan["unit_id"] or plan["revision_id"] not in unit["revision_ids"]:
        raise IntegrityError("committed logical unit revision does not match the transaction")
    revision_path = unit_root / "revisions" / plan["revision_id"] / "revision.json"
    revision = load_json_object(revision_path)
    validate_instance("development-revision.schema.json", revision)
    if revision["creation_id"] != transaction["creation_id"]:
        raise IntegrityError("committed logical unit belongs to another creation")
    if revision["request"]["sha256"] != transaction["request"]["sha256"]:
        raise IntegrityError("committed request differs from its transaction")
    artifacts: list[dict[str, Any]] = []
    for reference in revision["artifacts"]:
        manifest_path = revision_path.parent / reference["manifest"]["path"]
        if sha256_file(manifest_path) != reference["manifest"]["sha256"]:
            raise IntegrityError("committed artifact manifest SHA-256 mismatch")
        artifact = load_json_object(manifest_path)
        validate_instance("artifact.schema.json", artifact)
        driver = component_type_driver(artifact["kind"])
        if driver.type_id != unit_driver.type_id:
            raise IntegrityError("committed component type differs from its format-v1 Plugin")
        payload_root = require_inside(
            manifest_path.parent,
            artifact["payload_root"],
            f"artifact {artifact['artifact_id']} payload",
        )
        validate_regular_tree(payload_root)
        actual_files = {
            path.relative_to(payload_root).as_posix(): sha256_file(path)
            for path in payload_root.rglob("*")
            if path.is_file()
        }
        expected_files = {entry["path"]: entry["sha256"] for entry in artifact["files"]}
        if actual_files != expected_files:
            raise IntegrityError(
                f"committed artifact file inventory drifted: {artifact['artifact_id']}"
            )
        if tree_digest(payload_root) != artifact["tree_sha256"]:
            raise IntegrityError(f"committed artifact tree drifted: {artifact['artifact_id']}")
        driver.validate_payload(artifact, payload_root)
        artifacts.append(
            {
                "artifact_id": artifact["artifact_id"],
                "profile_id": artifact["profile_id"],
                "covered_clis": artifact["covered_clis"],
                "tree_sha256": artifact["tree_sha256"],
            }
        )
    return {
        "creation_id": transaction["creation_id"],
        "unit_id": plan["unit_id"],
        "revision_id": plan["revision_id"],
        "artifacts": artifacts,
    }


def _commit_update(root, target, transaction_root, request, plan, transaction, candidates):
    """Journal the append before publishing the new unit pointer (PSM-003)."""
    unit_path = require_inside(target, "unit.json", "update unit manifest")
    unit = load_json_object(unit_path)
    validate_instance("unit.schema.json", unit)
    if plan["revision_id"] in unit["revision_ids"]:
        return _committed_result(root, transaction, plan)
    if unit["current_revision_id"] != request["base_revision_id"]:
        raise CreationError("Skill 开发版本已变化，拒绝覆盖；请基于当前 revision 重新开始更新")
    for other_journal in _transaction_parent(root).glob("*/commit.json"):
        if other_journal.parent == transaction_root:
            continue
        other = load_json_object(other_journal)
        validate_config_instance("creation-update-commit.schema.json", other)
        validate_instance("unit.schema.json", other["unit"])
        if (
            other["unit"]["unit_id"] == plan["unit_id"]
            and other["unit"]["current_revision_id"] not in unit["revision_ids"]
        ):
            raise CreationError(
                f"该 Skill 有未完成的持久化更新，请先重试 commit：{other['creation_id']}"
            )
    journal_path = transaction_root / "commit.json"
    if journal_path.exists() or is_link(journal_path):
        journal = load_json_object(
            require_inside(transaction_root, "commit.json", "update journal")
        )
        validate_config_instance("creation-update-commit.schema.json", journal)
        validate_instance("unit.schema.json", journal["unit"])
    else:
        prepared = _prepare_unit(transaction_root, request, plan, transaction, candidates)
        next_unit = {
            **unit,
            "current_revision_id": plan["revision_id"],
            "revision_ids": [*unit["revision_ids"], plan["revision_id"]],
            "updated_at": utc_now(),
        }
        journal = {
            "schema_version": 1,
            "creation_id": transaction["creation_id"],
            "base_unit_sha256": sha256_file(unit_path),
            "revision_tree_sha256": tree_digest(prepared / "revisions" / plan["revision_id"]),
            "unit": next_unit,
        }
        validate_config_instance("creation-update-commit.schema.json", journal)
        write_new_json(journal_path, journal)
        fsync_tree(transaction_root)
    expected_unit = {
        **unit,
        "current_revision_id": plan["revision_id"],
        "revision_ids": [*unit["revision_ids"], plan["revision_id"]],
        "updated_at": journal["unit"]["updated_at"],
    }
    if (
        journal["creation_id"] != transaction["creation_id"]
        or journal["base_unit_sha256"] != sha256_file(unit_path)
        or journal["unit"] != expected_unit
    ):
        raise IntegrityError("update journal differs from its base unit or transaction")
    revision_target = require_inside(
        target, f"revisions/{plan['revision_id']}", "update revision", must_exist=False
    )
    if not revision_target.exists():
        prepared_revision = require_inside(
            transaction_root,
            f"prepared/{plan['unit_id']}/revisions/{plan['revision_id']}",
            "prepared update revision",
        )
        if tree_digest(prepared_revision) != journal["revision_tree_sha256"]:
            raise IntegrityError("prepared update revision has drifted")
        move_path(prepared_revision, revision_target)
        fsync_directory(revision_target.parent)
    if tree_digest(revision_target) != journal["revision_tree_sha256"]:
        raise IntegrityError("pending update revision has drifted")
    atomic_replace_json(unit_path, journal["unit"])
    return _committed_result(root, transaction, plan)


@lifecycle_write
def commit_creation(
    library_root: Path,
    creation_id: str,
    *,
    config_root: Path | None = None,
) -> dict[str, Any]:
    """Validate all candidates and atomically commit one new logical unit."""

    root = canonical_existing_root(library_root)
    transaction_root = _transaction_root(root, creation_id)
    transaction_path, transaction = _transaction_record(transaction_root)
    if transaction["library_id"] != load_json_object(root / "library.json")["library_id"]:
        raise IntegrityError("creation transaction library identity mismatch")
    if transaction["staging_root"] != str(transaction_root):
        raise IntegrityError("creation transaction staging root mismatch")

    formally_committed = False
    try:
        context = resolve_creation_context(root, config_root=config_root)
        request, plan = _validate_plan_and_request(root, transaction_root, transaction, context)
        if transaction["state"] == "ABORTED":
            raise CreationError(f"creation transaction is aborted: {creation_id}")
        if transaction["state"] == "COMMITTED":
            return {**_committed_result(root, transaction, plan), "idempotent": True}

        journaled = request.get("base_revision_id") and (transaction_root / "commit.json").exists()
        artifacts = [] if journaled else _validate_candidate_layout(transaction_root, plan)
        if transaction["state"] == "OPEN":
            transaction = _update_transaction(transaction_path, transaction, "GENERATED")
        if transaction["state"] not in {"GENERATED", "VALIDATED"}:
            raise CreationError(f"invalid creation transaction state: {transaction['state']}")
        candidates = [] if journaled else _validate_candidate_protocols(transaction_root, plan)
        if len(candidates) != len(artifacts):  # defensive invariant
            raise CreationError("candidate protocol result is incomplete")
        if transaction["state"] == "GENERATED":
            transaction = _update_transaction(transaction_path, transaction, "VALIDATED")

        with _unit_lock(root, plan["unit_id"]):
            target = root / "development" / "units" / plan["unit_id"]
            if request.get("base_revision_id"):
                recovered = _commit_update(
                    root, target, transaction_root, request, plan, transaction, candidates
                )
                formally_committed = True
            elif target.exists() or is_link(target):
                recovered = _committed_result(root, transaction, plan)
                formally_committed = True
            else:
                prepared = _prepare_unit(
                    transaction_root,
                    request,
                    plan,
                    transaction,
                    candidates,
                )
                move_path(prepared, target)
                fsync_directory(target.parent)
                formally_committed = True
                recovered = _committed_result(root, transaction, plan)

        transaction = _update_transaction(transaction_path, transaction, "COMMITTED")
        return {**recovered, "idempotent": False}
    except (CreationError, IntegrityError, OSError, PathSafetyError, SchemaValidationError) as exc:
        if not formally_committed and not (transaction_root / "commit.json").exists():
            _abort_after_failure(transaction_path, str(exc))
        raise


@lifecycle_write
def abort_creation(library_root: Path, creation_id: str) -> dict[str, Any]:
    """Abort an uncommitted creation transaction without deleting its evidence."""

    root = canonical_existing_root(library_root)
    transaction_root = _transaction_root(root, creation_id)
    transaction_path, transaction = _transaction_record(transaction_root)
    library_id = load_json_object(root / "library.json")["library_id"]
    if transaction["library_id"] != library_id:
        raise IntegrityError("creation transaction library identity mismatch")
    if transaction["staging_root"] != str(transaction_root):
        raise IntegrityError("creation transaction staging root mismatch")
    if transaction["state"] == "COMMITTED":
        raise CreationError(f"cannot abort a committed creation: {creation_id}")
    if (transaction_root / "commit.json").exists():
        raise CreationError("更新提交已进入持久化阶段，请重试 commit 完成恢复，不能 abort")
    if transaction["state"] == "ABORTED":
        return {"creation_id": creation_id, "state": "ABORTED", "idempotent": True}
    _update_transaction(
        transaction_path,
        transaction,
        "ABORTED",
        error={"code": "user-aborted", "message": "creation aborted before commit"},
    )
    return {"creation_id": creation_id, "state": "ABORTED", "idempotent": False}


__all__ = [
    "abort_creation",
    "begin_creation",
    "commit_creation",
    "resolve_library_root",
]
