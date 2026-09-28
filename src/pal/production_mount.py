"""Production projections, mounts, activation, rollback, and recovery.

The PAL active pointer is the only production-selection fact. Both targets
consume version-specific persistent marketplace plugins. Claude's controlled
runner also selects the verified projection explicitly with ``--plugin-dir``.
Private installations are bridged to the PAL pointer by a durable transition
record and can always be rebuilt from PAL-owned state.

Traceability: PRD-MOUNT-003, PRD-MOUNT-004, PRD-RELEASE-002;
ACC-008, ACC-011, ACC-012.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - guarded by the P0 platform gate
    fcntl = None

from . import __version__
from .adapters import require_compatible_cli
from .compatibility import CliCompatibility
from .components import SKILL_COMPONENT_TYPE_ID, component_type_driver
from .config_mount import resolve_config_root
from .errors import IntegrityError, PathSafetyError, ProductionError
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
)
from .library import doctor_development_context, doctor_library, load_json_object, utc_now
from .maintenance import lifecycle_write, require_no_cleanup
from .paths import (
    canonical_existing_root,
    normalize_relative_path,
    require_inside,
    require_safe_id,
    validate_regular_tree,
)
from .publishing import validate_production_version
from .schema_catalog import TARGET_CLIS, validate_instance
from .targets import target_driver

ACTIVE_SCHEMA = "active-production.schema.json"
MOUNT_SCHEMA = "production-mount.schema.json"
TRANSITION_SCHEMA = "production-transition.schema.json"
PRODUCTION_PLUGIN_PREFIX = "pal-production"
PHASE_ORDER = {
    "PREPARING_TARGET": 0,
    "TARGET_VERIFIED": 1,
    "SWAPPING_PRIVATE_PROJECTION": 2,
    "ACTIVE_POINTER_COMMITTED": 3,
}


@dataclass(frozen=True)
class TargetPlan:
    """All deterministic material needed to activate one production version."""

    library_root: Path
    config_root: Path
    library_id: str
    version_id: str
    validated: dict[str, Any]
    plugin_name: str
    marketplace_name: str
    projection_root: Path
    projection_files: dict[str, bytes]
    cli_projection_files: dict[str, dict[str, bytes]]
    cli_projection_digests: dict[str, str]
    mount_root: Path
    mount_files: dict[str, bytes]
    bundle: dict[str, Any]
    bundle_sha256: str
    active: dict[str, Any]
    active_sha256: str
    executables: dict[str, str]
    install_roots: dict[str, Path]
    install_files: dict[str, dict[str, bytes]]


def _mkdir_descendant(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PathSafetyError(f"production configuration path escapes config root: {path}") from exc
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.exists() or cursor.is_symlink():
            if cursor.is_symlink() or not cursor.is_dir():
                raise PathSafetyError(
                    f"production configuration path is not a real directory: {cursor}"
                )
        else:
            cursor.mkdir()


def _safe_remove_staging(path: Path, parent: Path, prefix: str) -> None:
    try:
        path.relative_to(parent)
    except ValueError as exc:  # pragma: no cover - defensive invariant
        raise PathSafetyError(f"refusing to clean production staging path: {path}") from exc
    if path.is_symlink() or not path.name.startswith(prefix):
        raise PathSafetyError(f"refusing to clean unexpected production staging path: {path}")
    shutil.rmtree(path)


def _files_digest(files: Mapping[str, bytes]) -> str:
    entries = [
        {"path": relative, "sha256": sha256_bytes(material)} for relative, material in files.items()
    ]
    entries.sort(key=lambda item: item["path"].encode("utf-8"))
    return sha256_bytes(canonical_json_bytes(entries))


def _verify_files(root: Path, expected: Mapping[str, bytes], label: str) -> None:
    if root.is_symlink() or not root.is_dir():
        raise IntegrityError(f"{label} is not a real directory: {root}")
    validate_regular_tree(root)
    actual = {path.relative_to(root).as_posix(): path for path in root.rglob("*") if path.is_file()}
    if set(actual) != set(expected):
        raise IntegrityError(f"{label} file inventory has drifted")
    for relative, material in expected.items():
        if actual[relative].read_bytes() != material:
            raise IntegrityError(f"{label} file has drifted: {relative}")
    if tree_digest(root) != _files_digest(expected):
        raise IntegrityError(f"{label} tree SHA-256 mismatch")


def _verify_installed_tree(actual: Path, expected: Path) -> None:
    """Verify delivered files, permitting only Python's adjacent bytecode cache."""
    actual = canonical_existing_root(actual)
    validate_regular_tree(actual)
    expected_files = {
        str(path.relative_to(expected)): path.read_bytes()
        for path in expected.rglob("*")
        if path.is_file()
    }
    delivered = set(expected_files)
    actual_files = {str(path.relative_to(actual)) for path in actual.rglob("*") if path.is_file()}
    for relative in actual_files - delivered:
        path = Path(relative)
        match = re.fullmatch(r"(.+)\.cpython-[0-9]+(?:\.opt-[0-9]+)?\.pyc", path.name)
        source = path.parent.parent / f"{match[1]}.py" if match else None
        if path.parent.name != "__pycache__" or source is None or str(source) not in delivered:
            raise IntegrityError(f"安装目录包含未交付文件：{relative}")
    for relative, material in expected_files.items():
        path = actual / relative
        if not path.is_file() or path.read_bytes() != material:
            raise IntegrityError(f"安装文件与 PAL 投影不一致：{relative}")


def _materialize_files(
    parent: Path,
    target: Path,
    expected: Mapping[str, bytes],
    *,
    prefix: str,
    label: str,
) -> bool:
    """Atomically create or validate one immutable directory.

    Returns ``True`` when a pre-existing object was reused.
    """

    if target.exists() or target.is_symlink():
        _verify_files(target, expected, label)
        return True
    temporary = Path(tempfile.mkdtemp(dir=parent, prefix=prefix, suffix=".tmp"))
    committed = False
    try:
        for relative, material in expected.items():
            normalize_relative_path(relative, f"{label} file")
            write_new_bytes(temporary / relative, material)
        _verify_files(temporary, expected, label)
        fsync_tree(temporary)
        os.rename(temporary, target)
        fsync_directory(parent)
        committed = True
    finally:
        if not committed and temporary.exists():
            _safe_remove_staging(temporary, parent, prefix)
    return False


def _slug(value: str, *, maximum: int = 24) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "library"
    normalized = normalized[:maximum].rstrip("-") or "library"
    return f"{normalized}-{sha256_bytes(value.encode('utf-8'))[:8]}"


def _stable_plugin_name(library_id: str) -> str:
    token = library_id
    if len(token) > 48 or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", token) is None:
        token = _slug(library_id, maximum=39)
    return f"pal-{token}"


def _projection_identity(
    library_id: str, version_id: str, *, stable: bool = False
) -> tuple[str, str]:
    library_token = _slug(library_id)
    version_token = sha256_bytes(version_id.encode("utf-8"))[:12]
    plugin_name = f"{PRODUCTION_PLUGIN_PREFIX}-{library_token}-{version_token}"
    marketplace_name = f"{plugin_name}-market"
    if stable:
        plugin_name = _stable_plugin_name(library_id)
    require_safe_id(plugin_name, "production plugin ID")
    require_safe_id(marketplace_name, "production marketplace ID")
    return plugin_name, marketplace_name


def _semver_with_cachebuster(version_id: str, generator_version: str) -> str:
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:[-+].*)?", generator_version)
    if match is None:
        raise ProductionError(
            f"PAL version is not compatible with plugin SemVer: {generator_version}"
        )
    token = sha256_bytes(version_id.encode("utf-8"))[:12]
    return f"{match.group(1)}+codex.{token}"


def _selected_payload_files(
    validated: dict[str, Any],
    cli_id: str,
) -> dict[str, bytes]:
    version_root = validated["manifest_path"].parent
    source_artifacts = {
        item["artifact"]["artifact_id"]: item["artifact"]
        for release in validated["releases"]
        for item in release["artifacts"]
    }
    files: dict[str, bytes] = {}
    selected_kinds: set[str] = set()
    for artifact in validated["production"]["artifacts"]:
        if cli_id not in artifact["covered_clis"]:
            continue
        source = source_artifacts.get(artifact["artifact_id"])
        if source is None:
            raise IntegrityError(
                f"production artifact has no release source: {artifact['artifact_id']}"
            )
        driver = component_type_driver(source["kind"])
        selected_kinds.add(driver.type_id)
        payload_root = require_inside(
            version_root,
            artifact["payload_root"],
            f"production artifact {artifact['artifact_id']} payload",
        )
        if tree_digest(payload_root) != artifact["tree_sha256"]:
            raise IntegrityError(
                f"production artifact projection source drifted: {artifact['artifact_id']}"
            )
        source_files = {entry["path"]: entry["sha256"] for entry in source["files"]}
        for relative, material in driver.projection_files(source, payload_root).items():
            normalize_relative_path(relative, "production projection payload path")
            if source_files.get(relative) != sha256_bytes(material):
                raise IntegrityError(
                    f"artifact driver projected material outside its immutable payload: "
                    f"{artifact['artifact_id']}:{relative}"
                )
            if relative in files:
                raise ProductionError(
                    f"production artifacts collide in the {cli_id} projection: {relative}"
                )
            files[relative] = material
    if not validated["production"]["releases"]:
        return {}
    if not files:
        raise ProductionError(f"production projection contains no artifact for {cli_id}")
    if selected_kinds != {SKILL_COMPONENT_TYPE_ID}:
        raise ProductionError(
            f"v1 target projection only supports registered Skill artifacts for {cli_id}"
        )
    return files


def _production_plugin_version(validated: dict[str, Any], cli_id: str) -> str:
    """Preserve each target's original immutable projection version contract."""
    production = validated["production"]
    if production["schema_version"] == 2 or cli_id == "codex":
        return _semver_with_cachebuster(
            production["production_version_id"], production["pal_version"]
        )
    return production["pal_version"]


def _projection_files(
    validated: dict[str, Any],
    library_id: str,
    version_id: str,
) -> tuple[str, str, dict[str, dict[str, bytes]]]:
    stable = validated["production"]["schema_version"] == 2
    plugin_name, marketplace_name = _projection_identity(library_id, version_id, stable=stable)
    by_cli: dict[str, dict[str, bytes]] = {}
    for cli_id in TARGET_CLIS:
        payload = _selected_payload_files(validated, cli_id)
        _, files = target_driver(cli_id).project_plugin(
            plugin_name=plugin_name,
            marketplace_name=marketplace_name if cli_id == "codex" else None,
            plugin_version=_production_plugin_version(validated, cli_id),
            description="PAL immutable production Skill projection",
            marketplace_display_name="PAL Production",
            display_name=f"PAL Production {version_id}",
            short_description="Use Skills from one activated PAL production version.",
            long_description=(
                "A version-specific projection generated from an immutable PAL production "
                "aggregate and activated through the PAL transition protocol."
            ),
            default_prompt="Use the applicable activated PAL production Skill.",
            payload_files=payload,
        )
        by_cli[cli_id] = files
    return plugin_name, marketplace_name, by_cli


def _merge_projection_files(by_cli: Mapping[str, Mapping[str, bytes]]) -> dict[str, bytes]:
    return {
        f"{cli_id}/{relative}": material
        for cli_id in TARGET_CLIS
        for relative, material in by_cli[cli_id].items()
    }


def _reject_development_reference(value: Any, label: str) -> None:
    if isinstance(value, str) and "development/" in value:
        raise IntegrityError(f"{label} contains a forbidden development path")
    if isinstance(value, list):
        for item in value:
            _reject_development_reference(item, label)
    elif isinstance(value, dict):
        for item in value.values():
            _reject_development_reference(item, label)


def _detect_supported_executables() -> dict[str, str]:
    return {
        cli_id: compatibility.executable
        for cli_id, compatibility in _detect_target_compatibilities().items()
    }


def _detect_target_compatibilities() -> dict[str, CliCompatibility]:
    return {cli_id: require_compatible_cli(cli_id) for cli_id in TARGET_CLIS}


def _mount_records(
    *,
    validated: dict[str, Any],
    library_id: str,
    version_id: str,
    projection_root: Path,
    projection_digests: Mapping[str, str],
    cli_versions: Mapping[str, str],
    verified_at: str,
    generator_version: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, bytes]]:
    production = validated["production"]
    releases = [
        {
            "release_id": item["release_id"],
            "manifest_sha256": item["manifest"]["sha256"],
        }
        for item in production["releases"]
    ]
    records: dict[str, dict[str, Any]] = {}
    files: dict[str, bytes] = {}
    for cli_id in TARGET_CLIS:
        artifacts = [
            {
                "artifact_id": item["artifact_id"],
                "profile_id": item["profile_id"],
                "covered_clis": item["covered_clis"],
                "tree_sha256": item["tree_sha256"],
            }
            for item in production["artifacts"]
            if cli_id in item["covered_clis"]
        ]
        record = {
            "schema_version": production["schema_version"],
            "mount_kind": "production",
            "record_kind": "cli",
            "mount_id": f"production-{library_id}-{version_id}-{cli_id}",
            "library_id": library_id,
            "production_version_id": version_id,
            "production_manifest_sha256": validated["manifest_sha256"],
            "cli_id": cli_id,
            "cli_version": cli_versions[cli_id],
            "releases": releases,
            "artifacts": artifacts,
            "projection_root": str(projection_root / cli_id),
            "projection_tree_sha256": projection_digests[cli_id],
            "effective_on": "new-session",
            "pal_version": generator_version,
            "verified_at": verified_at,
        }
        validate_instance(MOUNT_SCHEMA, record)
        _reject_development_reference(record, f"{cli_id} production mount")
        records[cli_id] = record
        files[f"{cli_id}.json"] = formatted_json_bytes(record)

    bundle = {
        "schema_version": production["schema_version"],
        "mount_kind": "production",
        "record_kind": "bundle",
        "mount_id": f"production-{library_id}-{version_id}",
        "library_id": library_id,
        "production_version_id": version_id,
        "production_manifest_sha256": validated["manifest_sha256"],
        "cli_records": [
            {
                "cli_id": cli_id,
                "path": f"{cli_id}.json",
                "sha256": sha256_bytes(files[f"{cli_id}.json"]),
            }
            for cli_id in TARGET_CLIS
        ],
        "pal_version": generator_version,
        "verified_at": verified_at,
    }
    validate_instance(MOUNT_SCHEMA, bundle)
    _reject_development_reference(bundle, "production mount bundle")
    files["bundle.json"] = formatted_json_bytes(bundle)
    return records, bundle, files


def _mount_metadata(
    mount_root: Path,
    fallback_timestamp: str,
    detected_versions: Mapping[str, str],
) -> tuple[str, dict[str, str], str]:
    if not (mount_root.exists() or mount_root.is_symlink()):
        if set(detected_versions) != set(TARGET_CLIS):
            raise ProductionError(
                "new production mount requires compatibility evidence for all targets"
            )
        return fallback_timestamp, dict(detected_versions), __version__
    if mount_root.is_symlink() or not mount_root.is_dir():
        raise IntegrityError(f"production mount root is invalid: {mount_root}")
    bundle_path = mount_root / "bundle.json"
    bundle = load_json_object(bundle_path)
    validate_instance(MOUNT_SCHEMA, bundle)
    timestamp = bundle["verified_at"]
    if not isinstance(timestamp, str):  # pragma: no cover - schema establishes this
        raise IntegrityError("production mount verified_at is invalid")
    versions: dict[str, str] = {}
    for cli_id in TARGET_CLIS:
        record = load_json_object(mount_root / f"{cli_id}.json")
        validate_instance(MOUNT_SCHEMA, record)
        if (
            record.get("cli_id") != cli_id
            or record.get("verified_at") != timestamp
            or record["pal_version"] != bundle["pal_version"]
        ):
            raise IntegrityError("production mount CLI metadata differs from its bundle")
        versions[cli_id] = record["cli_version"]
    return timestamp, versions, bundle["pal_version"]


def _build_target_plan(
    library_root: Path,
    config_root: Path,
    library_id: str,
    version_id: str,
    *,
    activation_time: str,
    executables: dict[str, str] | None = None,
    compatibilities: Mapping[str, CliCompatibility] | None = None,
    create_layout: bool = True,
) -> TargetPlan:
    version_id = require_safe_id(version_id, "production version ID")
    if create_layout and not (library_root / "production/versions" / version_id).is_dir():
        raise ProductionError("生产内容仅存于旧证据归档或已缺失；请先正常发布，再同步到 CLI")
    validated = validate_production_version(library_root, version_id)
    plugin_name, marketplace_name, by_cli = _projection_files(
        validated,
        library_id,
        version_id,
    )
    projection_files = _merge_projection_files(by_cli)
    projection_digests = {cli_id: _files_digest(by_cli[cli_id]) for cli_id in TARGET_CLIS}
    projection_parent = config_root / "projections" / library_id / "versions"
    mount_parent = config_root / "mounts" / "production" / library_id / "versions"
    if create_layout:
        _mkdir_descendant(projection_parent, config_root)
        _mkdir_descendant(mount_parent, config_root)
    elif any(
        parent.is_symlink() or not parent.is_dir() for parent in (projection_parent, mount_parent)
    ):
        raise IntegrityError("production mount layout is incomplete")
    projection_root = projection_parent / version_id
    mount_root = mount_parent / version_id
    install_files: dict[str, dict[str, bytes]] = {}
    install_roots: dict[str, Path] = {}
    for cli_id in TARGET_CLIS:
        files = target_driver(cli_id).project_install_root(
            plugin_name=plugin_name,
            marketplace_name=marketplace_name,
            description="PAL immutable production Skill projection",
            projection_files=by_cli[cli_id],
        )
        if not files:
            continue
        install_parent = require_inside(
            config_root,
            f"installs/{cli_id}/{library_id}/versions",
            "production install layout",
            must_exist=False,
        )
        if create_layout:
            _mkdir_descendant(install_parent, config_root)
        elif install_parent.exists() and not install_parent.is_dir():
            raise IntegrityError("production install layout is not a directory")
        # Historical mount/usage validation concerns immutable projections.
        # The later-added installation wrapper need not exist and must not be
        # created by that read-only path. Activation materializes it separately.
        install_files[cli_id] = files
        install_roots[cli_id] = install_parent / version_id
    if compatibilities is None and executables is None:
        compatibilities = _detect_target_compatibilities()
    detected_versions = {
        cli_id: compatibility.actual_version
        for cli_id, compatibility in (compatibilities or {}).items()
    }
    verified_at, mount_versions, mount_generator = _mount_metadata(
        mount_root,
        activation_time,
        detected_versions,
    )
    _, bundle, mount_files = _mount_records(
        validated=validated,
        library_id=library_id,
        version_id=version_id,
        projection_root=projection_root,
        projection_digests=projection_digests,
        cli_versions=mount_versions,
        verified_at=verified_at,
        generator_version=mount_generator,
    )
    bundle_sha256 = sha256_bytes(mount_files["bundle.json"])
    manifest_relative = validated["manifest_path"].relative_to(library_root).as_posix()
    active = {
        "schema_version": 1,
        "library_id": library_id,
        "active_production_version_id": version_id,
        "production_manifest": {
            "path": manifest_relative,
            "sha256": validated["manifest_sha256"],
        },
        "production_mount_bundle": {
            "path": str(mount_root / "bundle.json"),
            "sha256": bundle_sha256,
        },
        "activated_at": activation_time,
    }
    validate_instance(ACTIVE_SCHEMA, active)
    return TargetPlan(
        library_root=library_root,
        config_root=config_root,
        library_id=library_id,
        version_id=version_id,
        validated=validated,
        plugin_name=plugin_name,
        marketplace_name=marketplace_name,
        projection_root=projection_root,
        projection_files=projection_files,
        cli_projection_files=by_cli,
        cli_projection_digests=projection_digests,
        mount_root=mount_root,
        mount_files=mount_files,
        bundle=bundle,
        bundle_sha256=bundle_sha256,
        active=active,
        active_sha256=sha256_bytes(formatted_json_bytes(active)),
        executables=(
            executables
            if executables is not None
            else {
                cli_id: compatibility.executable
                for cli_id, compatibility in (compatibilities or {}).items()
            }
        ),
        install_roots=install_roots,
        install_files=install_files,
    )


def _materialize_plan(plan: TargetPlan) -> tuple[bool, bool]:
    reused_projection = _materialize_files(
        plan.projection_root.parent,
        plan.projection_root,
        plan.projection_files,
        prefix=f".projection-{plan.version_id}-",
        label=f"production projection {plan.version_id}",
    )
    reused_mount = _materialize_files(
        plan.mount_root.parent,
        plan.mount_root,
        plan.mount_files,
        prefix=f".mount-{plan.version_id}-",
        label=f"production mount {plan.version_id}",
    )
    for cli_id, root in plan.install_roots.items():
        _materialize_files(
            root.parent,
            root,
            plan.install_files[cli_id],
            prefix=f".install-{plan.version_id}-",
            label=f"{cli_id} production install root {plan.version_id}",
        )
    return reused_projection, reused_mount


def _plugin_root(plan: TargetPlan, cli_id: str) -> Path:
    if cli_id == "claude-code":
        return plan.projection_root / f"claude-code/plugins/{plan.plugin_name}"
    return plan.projection_root / f"codex/marketplace/plugins/{plan.plugin_name}"


def _marketplace_root(plan: TargetPlan) -> Path:
    return plan.projection_root / "codex/marketplace"


def _run_claude_validation(plan: TargetPlan) -> None:
    process = subprocess.run(
        [
            plan.executables["claude-code"],
            "plugin",
            "validate",
            "--strict",
            str(_plugin_root(plan, "claude-code")),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip()
        raise ProductionError(f"Claude production plugin validation failed: {detail}")


def _run_json(arguments: list[str], label: str) -> dict[str, Any]:
    process = subprocess.run(arguments, check=False, capture_output=True, text=True)
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip()
        raise ProductionError(f"{label} failed: {detail}")
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ProductionError(f"{label} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ProductionError(f"{label} returned a non-object JSON envelope")
    return value


def _run_text(arguments: list[str], label: str) -> None:
    """Run one target mutation that reports success through its exit code only."""

    process = subprocess.run(arguments, check=False, capture_output=True, text=True)
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or f"exit {process.returncode}"
        raise ProductionError(f"{label} failed: {detail}")


def _run_json_array(arguments: list[str], label: str) -> list[dict[str, Any]]:
    """Read one Claude ``--json`` listing, whose envelope is a bare array."""

    process = subprocess.run(arguments, check=False, capture_output=True, text=True)
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip()
        raise ProductionError(f"{label} failed: {detail}")
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ProductionError(f"{label} returned invalid JSON") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ProductionError(f"{label} returned invalid JSON entries")
    return value


def _codex_marketplaces(executable: str) -> list[dict[str, Any]]:
    payload = _run_json(
        [executable, "plugin", "marketplace", "list", "--json"],
        "Codex marketplace list",
    )
    entries = payload.get("marketplaces")
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise ProductionError("Codex marketplace list returned invalid entries")
    return entries


def _codex_plugins(executable: str) -> list[dict[str, Any]]:
    payload = _run_json(
        [executable, "plugin", "list", "--json"],
        "Codex plugin list",
    )
    entries = payload.get("installed")
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise ProductionError("Codex plugin list returned invalid entries")
    return entries


def _plugin_id(plan: TargetPlan) -> str:
    return f"{plan.plugin_name}@{plan.marketplace_name}"


def _verify_codex_installed(plan: TargetPlan) -> dict[str, Any]:
    entries = _codex_plugins(plan.executables["codex"])
    plugin_id = _plugin_id(plan)
    matching = [
        item for item in entries if item.get("pluginId") == plugin_id and item.get("installed")
    ]
    if len(matching) != 1:
        raise ProductionError(f"Codex production plugin is not uniquely installed: {plugin_id}")

    if matching[0].get("enabled") is not True:
        raise ProductionError("Codex 业务 Skill 插件未启用，请在 Codex 中启用后重新检查")

    prefix = f"{PRODUCTION_PLUGIN_PREFIX}-{_slug(plan.library_id)}-"
    conflicting = [
        item.get("pluginId")
        for item in entries
        if item.get("installed")
        and isinstance(item.get("name"), str)
        and (
            item["name"].startswith(prefix)
            or item["name"] in {plan.plugin_name, _stable_plugin_name(plan.library_id)}
        )
        and item.get("pluginId") != plugin_id
    ]
    if conflicting:
        raise ProductionError(
            f"conflicting PAL production plugins remain installed: {sorted(conflicting)}"
        )

    source = matching[0].get("source")
    source_path = source.get("path") if isinstance(source, dict) else None
    if not isinstance(source_path, str) or not Path(source_path).is_absolute():
        raise ProductionError("Codex installed production plugin has no absolute source path")
    installed_root = canonical_existing_root(Path(source_path))
    expected_root = _plugin_root(plan, "codex")
    _verify_installed_tree(installed_root, expected_root)
    return matching[0]


def _codex_cached_plugin_root(
    plan: TargetPlan,
    installation: dict[str, Any],
) -> Path:
    """Resolve and verify the private cache Codex actually exposes to a run."""

    expected_version = _semver_with_cachebuster(
        plan.version_id, plan.validated["production"]["pal_version"]
    )
    if installation.get("version") != expected_version:
        raise ProductionError("Codex installed production plugin version is invalid")
    configured_home = os.environ.get("CODEX_HOME")
    codex_home = Path(configured_home) if configured_home else Path.home() / ".codex"
    if not codex_home.is_absolute():
        raise PathSafetyError("Codex home must be an absolute path")
    cache_root = (
        codex_home
        / "plugins"
        / "cache"
        / plan.marketplace_name
        / plan.plugin_name
        / expected_version
    )
    installed_root = canonical_existing_root(cache_root)
    validate_regular_tree(installed_root)
    expected_root = _plugin_root(plan, "codex")
    _verify_installed_tree(installed_root, expected_root)
    return installed_root


def _ensure_codex_marketplace(plan: TargetPlan) -> None:
    entries = _codex_marketplaces(plan.executables["codex"])
    matching = [item for item in entries if item.get("name") == plan.marketplace_name]
    if len(matching) > 1:
        raise ProductionError(f"Codex marketplace is duplicated: {plan.marketplace_name}")
    expected = _marketplace_root(plan).resolve(strict=True)
    if matching:
        actual_value = matching[0].get("root")
        if not isinstance(actual_value, str):
            raise ProductionError(f"Codex marketplace root is invalid: {plan.marketplace_name}")
        actual = Path(actual_value).resolve(strict=False)
        if actual != expected:
            raise ProductionError(
                f"Codex marketplace name points to another root: {plan.marketplace_name}"
            )
        return
    _run_json(
        [
            plan.executables["codex"],
            "plugin",
            "marketplace",
            "add",
            str(expected),
            "--json",
        ],
        "Codex marketplace add",
    )


def _install_codex(plan: TargetPlan) -> None:
    for entry in _codex_plugins(plan.executables["codex"]):
        if (
            entry.get("installed")
            and entry.get("name") == plan.plugin_name
            and entry.get("pluginId") != _plugin_id(plan)
        ):
            raise ProductionError("同名插件已由其他来源安装，拒绝覆盖；请先核对插件来源")
    _ensure_codex_marketplace(plan)
    _run_json(
        [
            plan.executables["codex"],
            "plugin",
            "add",
            _plugin_id(plan),
            "--json",
        ],
        "Codex production plugin install",
    )
    _verify_codex_installed(plan)


def _remove_codex(plan: TargetPlan) -> None:
    plugin_id = _plugin_id(plan)
    matching = [
        item
        for item in _codex_plugins(plan.executables["codex"])
        if item.get("pluginId") == plugin_id and item.get("installed")
    ]
    if len(matching) > 1:
        raise ProductionError(f"Codex production plugin is duplicated: {plugin_id}")
    if matching:
        _run_json(
            [
                plan.executables["codex"],
                "plugin",
                "remove",
                plugin_id,
                "--json",
            ],
            "Codex production plugin removal",
        )
    remaining = [
        item
        for item in _codex_plugins(plan.executables["codex"])
        if item.get("pluginId") == plugin_id and item.get("installed")
    ]
    if remaining:
        raise ProductionError(f"Codex production plugin removal was incomplete: {plugin_id}")


def _claude_marketplace_name(plan: TargetPlan) -> str:
    return plan.marketplace_name


def _claude_plugin_id(plan: TargetPlan) -> str:
    return f"{plan.plugin_name}@{_claude_marketplace_name(plan)}"


def _claude_marketplaces(executable: str) -> list[dict[str, Any]]:
    return _run_json_array(
        [executable, "plugin", "marketplace", "list", "--json"],
        "Claude marketplace list",
    )


def _claude_plugins(executable: str) -> list[dict[str, Any]]:
    return _run_json_array(
        [executable, "plugin", "list", "--json"],
        "Claude plugin list",
    )


def _verify_claude_installed(plan: TargetPlan) -> dict[str, Any]:
    plugin_id = _claude_plugin_id(plan)
    entries = _claude_plugins(plan.executables["claude-code"])
    matching = [
        item for item in entries if item.get("id") == plugin_id and item.get("enabled") is True
    ]
    if len(matching) != 1:
        raise ProductionError(f"Claude production plugin is not uniquely installed: {plugin_id}")
    conflicting = {
        str(item.get("id"))
        for item in entries
        if item.get("id") != plugin_id
        and (
            str(item.get("id", "")).startswith(
                f"{PRODUCTION_PLUGIN_PREFIX}-{_slug(plan.library_id)}-"
            )
            or str(item.get("id", "")).startswith(f"{plan.plugin_name}@")
            or str(item.get("id", "")).startswith(f"{_stable_plugin_name(plan.library_id)}@")
        )
    }
    if conflicting:
        raise ProductionError(
            f"conflicting PAL production plugins remain installed: {sorted(conflicting)}"
        )
    installation = matching[0]
    install_path = installation.get("installPath")
    if not isinstance(install_path, str) or not install_path:
        raise ProductionError("Claude installed production plugin has no install path")
    installed_root = canonical_existing_root(Path(install_path))
    validate_regular_tree(installed_root)
    _verify_installed_tree(installed_root, _plugin_root(plan, "claude-code"))
    return installation


def _claude_installed_plugin_root(plan: TargetPlan, installation: Mapping[str, Any]) -> Path:
    """Return Claude's own installed copy, already digest-checked against PAL."""

    install_path = installation.get("installPath")
    if not isinstance(install_path, str) or not install_path:
        raise ProductionError("Claude installed production plugin has no install path")
    return canonical_existing_root(Path(install_path))


def _ensure_claude_marketplace(plan: TargetPlan) -> None:
    marketplace_name = _claude_marketplace_name(plan)
    entries = _claude_marketplaces(plan.executables["claude-code"])
    matching = [item for item in entries if item.get("name") == marketplace_name]
    if len(matching) > 1:
        raise ProductionError(f"Claude marketplace is duplicated: {marketplace_name}")
    expected = plan.install_roots["claude-code"].resolve(strict=True)
    if matching:
        actual_value = matching[0].get("path")
        if not isinstance(actual_value, str):
            raise ProductionError(f"Claude marketplace path is invalid: {marketplace_name}")
        if Path(actual_value).resolve(strict=False) != expected:
            raise ProductionError(
                f"Claude marketplace name points to another root: {marketplace_name}"
            )
        return
    _run_text(
        [
            plan.executables["claude-code"],
            "plugin",
            "marketplace",
            "add",
            str(expected),
            "--scope",
            "user",
        ],
        "Claude marketplace add",
    )


def _install_claude(plan: TargetPlan) -> None:
    for entry in _claude_plugins(plan.executables["claude-code"]):
        if str(entry.get("id", "")).startswith(f"{plan.plugin_name}@") and entry.get(
            "id"
        ) != _claude_plugin_id(plan):
            raise ProductionError("同名插件已由其他来源安装，拒绝覆盖；请先核对插件来源")
    _ensure_claude_marketplace(plan)
    _run_text(
        [
            plan.executables["claude-code"],
            "plugin",
            "install",
            _claude_plugin_id(plan),
            "--scope",
            "user",
            "--yes",
        ],
        "Claude production plugin install",
    )
    _verify_claude_installed(plan)


def _remove_claude(plan: TargetPlan) -> None:
    plugin_id = _claude_plugin_id(plan)
    executable = plan.executables["claude-code"]
    if any(item.get("id") == plugin_id for item in _claude_plugins(executable)):
        _run_text(
            [executable, "plugin", "uninstall", plugin_id],
            "Claude production plugin removal",
        )
    if any(item.get("id") == plugin_id for item in _claude_plugins(executable)):
        raise ProductionError(f"Claude production plugin removal was incomplete: {plugin_id}")
    marketplace_name = _claude_marketplace_name(plan)
    if any(item.get("name") == marketplace_name for item in _claude_marketplaces(executable)):
        _run_text(
            [executable, "plugin", "marketplace", "remove", marketplace_name],
            "Claude marketplace removal",
        )


def _installable_clis(plan: TargetPlan) -> tuple[str, ...]:
    """Return the CLIs whose persistent installation PAL drives for this plan."""

    return tuple(cli_id for cli_id in TARGET_CLIS if plan.executables.get(cli_id))


def _install_target(plan: TargetPlan, cli_id: str) -> None:
    if cli_id == "codex":
        _install_codex(plan)
        return
    _install_claude(plan)


def _remove_target(plan: TargetPlan, cli_id: str) -> None:
    if cli_id == "codex":
        _remove_codex(plan)
        return
    _remove_claude(plan)


def _verify_installed(plan: TargetPlan, cli_id: str) -> dict[str, Any]:
    if cli_id == "codex":
        return _verify_codex_installed(plan)
    return _verify_claude_installed(plan)


def _target_plugin_id(plan: TargetPlan, cli_id: str) -> str:
    if cli_id == "codex":
        return _plugin_id(plan)
    return _claude_plugin_id(plan)


def _native_verify(plan: TargetPlan) -> None:
    _verify_files(
        plan.projection_root,
        plan.projection_files,
        f"production projection {plan.version_id}",
    )
    _run_claude_validation(plan)
    # The Codex manifest/marketplace and complete payload are compared to the
    # deterministic expected byte map above.  Installation occurs only after
    # TARGET_VERIFIED is durable.


def _read_active(root: Path, library_id: str) -> tuple[dict[str, Any], str]:
    active_path = require_inside(root, "production/active.json", "active production pointer")
    active = load_json_object(active_path)
    validate_instance(ACTIVE_SCHEMA, active)
    if active["library_id"] != library_id:
        raise IntegrityError("active production pointer library identity mismatch")
    return active, sha256_file(active_path)


def _empty_active(library_id: str) -> dict[str, Any]:
    value = {
        "schema_version": 1,
        "library_id": library_id,
        "active_production_version_id": None,
        "production_manifest": None,
        "production_mount_bundle": None,
        "activated_at": None,
    }
    validate_instance(ACTIVE_SCHEMA, value)
    return value


def _plan_for_active(
    root: Path,
    config_root: Path,
    library_id: str,
    active: dict[str, Any],
    executables: dict[str, str],
) -> TargetPlan:
    version_id = active["active_production_version_id"]
    if version_id is None or active["activated_at"] is None:
        raise ProductionError("active production pointer is empty")
    plan = _build_target_plan(
        root,
        config_root,
        library_id,
        version_id,
        activation_time=active["activated_at"],
        executables=executables,
    )
    if plan.active != active:
        raise IntegrityError("active production pointer differs from its immutable closure")
    return plan


def validate_production_mount(
    library_root: Path,
    version_id: str,
    *,
    config_root: Path | None = None,
) -> dict[str, Any]:
    """Validate a production bundle and both immutable CLI projections."""

    root = canonical_existing_root(library_root)
    context = doctor_development_context(root)
    pal_config_root = resolve_config_root(config_root, create=False)
    version_id = require_safe_id(version_id, "production version ID")
    mount_root = (
        pal_config_root / "mounts" / "production" / context["library_id"] / "versions" / version_id
    )
    if mount_root.is_symlink() or not mount_root.is_dir():
        raise IntegrityError(f"production mount does not exist: {mount_root}")
    bundle = load_json_object(mount_root / "bundle.json")
    validate_instance(MOUNT_SCHEMA, bundle)
    activation_time = bundle["verified_at"]
    plan = _build_target_plan(
        root,
        pal_config_root,
        context["library_id"],
        version_id,
        activation_time=activation_time,
        executables={},
        create_layout=False,
    )
    _verify_files(plan.projection_root, plan.projection_files, "production projection")
    _verify_files(plan.mount_root, plan.mount_files, "production mount")
    if plan.bundle != bundle:
        raise IntegrityError("production mount bundle differs from its production closure")
    return {
        "library_id": context["library_id"],
        "production_version_id": version_id,
        "production_manifest_sha256": plan.validated["manifest_sha256"],
        "bundle": str(plan.mount_root / "bundle.json"),
        "bundle_sha256": plan.bundle_sha256,
        "projection_roots": {cli_id: str(plan.projection_root / cli_id) for cli_id in TARGET_CLIS},
        "projection_tree_sha256": plan.cli_projection_digests,
        "plugin_name": plan.plugin_name,
        "marketplace_name": plan.marketplace_name,
    }


def _validate_stable_active(
    root: Path,
    config_root: Path,
    library_id: str,
    *,
    verify_installation: bool,
    executables: dict[str, str],
) -> dict[str, Any]:
    active, active_sha256 = _read_active(root, library_id)
    if active["active_production_version_id"] is None:
        if active != _empty_active(library_id):
            raise IntegrityError("empty active production pointer is not canonical")
        return {"active": active, "active_sha256": active_sha256, "plan": None}
    plan = _plan_for_active(root, config_root, library_id, active, executables)
    validate_production_mount(root, plan.version_id, config_root=config_root)
    _native_verify(plan)
    if verify_installation:
        for cli_id in _installable_clis(plan):
            _verify_installed(plan, cli_id)
    return {"active": active, "active_sha256": active_sha256, "plan": plan}


@contextmanager
def _activation_lock(config_root: Path, library_id: str) -> Iterator[None]:
    if fcntl is None:  # pragma: no cover - doctor already rejects this platform
        raise ProductionError("production activation requires the POSIX lock backend")
    lock_root = config_root / "locks"
    _mkdir_descendant(lock_root, config_root)
    lock_path = lock_root / f"{library_id}.activation.lock"
    if lock_path.is_symlink():
        raise PathSafetyError(f"production activation lock cannot be a symlink: {lock_path}")
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o644,
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProductionError("production activation is locked by another operation") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _transition_path(config_root: Path, library_id: str) -> Path:
    parent = config_root / "transitions" / "production"
    _mkdir_descendant(parent, config_root)
    return parent / f"{library_id}.json"


def _projection_snapshot(plan: TargetPlan, cli_id: str, *, installed: bool) -> dict[str, Any]:
    return {
        "plugin_id": plan.plugin_name,
        "marketplace_id": plan.marketplace_name if cli_id == "codex" else None,
        "path": str(plan.projection_root / cli_id),
        "tree_sha256": plan.cli_projection_digests[cli_id],
        "installed": installed,
    }


def _new_transition(
    *,
    library_id: str,
    old_state: dict[str, Any],
    target: TargetPlan,
    created_at: str,
    old_codex_installed: bool = True,
) -> dict[str, Any]:
    old_plan = old_state["plan"]
    transition = {
        "schema_version": 1,
        "transaction_id": f"activation-{secrets.token_hex(12)}",
        "library_id": library_id,
        "from_version_id": (old_state["active"]["active_production_version_id"]),
        "to_version_id": target.version_id,
        "phase": "PREPARING_TARGET",
        "old_active_sha256": old_state["active_sha256"],
        "new_active_sha256": target.active_sha256,
        "clis": {
            cli_id: {
                "old_projection": (
                    _projection_snapshot(
                        old_plan,
                        cli_id,
                        installed=cli_id == "codex" and old_codex_installed,
                    )
                    if old_plan is not None
                    else None
                ),
                "target_projection": _projection_snapshot(target, cli_id, installed=False),
                "install_status": "unchanged",
            }
            for cli_id in TARGET_CLIS
        },
        "recovery_action": "restore-old",
        "created_at": created_at,
        "updated_at": created_at,
        "process": {"pid": os.getpid(), "hostname": socket.gethostname()},
    }
    validate_instance(TRANSITION_SCHEMA, transition)
    return transition


def _write_transition(path: Path, transition: dict[str, Any]) -> None:
    validate_instance(TRANSITION_SCHEMA, transition)
    atomic_replace_json(path, transition)


def _advance_transition(
    path: Path,
    transition: dict[str, Any],
    phase: str,
    *,
    recovery_action: str | None = None,
    install_status: Mapping[str, str] | None = None,
    target_installed: Mapping[str, bool] | None = None,
    old_installed: Mapping[str, bool] | None = None,
) -> None:
    if phase not in PHASE_ORDER or PHASE_ORDER[phase] < PHASE_ORDER[transition["phase"]]:
        raise ProductionError(f"invalid production transition phase advance: {phase}")
    transition["phase"] = phase
    transition["updated_at"] = utc_now()
    if recovery_action is not None:
        transition["recovery_action"] = recovery_action
    if phase == "TARGET_VERIFIED":
        for cli_id in TARGET_CLIS:
            transition["clis"][cli_id]["install_status"] = "offline-verified"
    for cli_id, status in (install_status or {}).items():
        transition["clis"][cli_id]["install_status"] = status
    for cli_id, installed in (target_installed or {}).items():
        transition["clis"][cli_id]["target_projection"]["installed"] = installed
    for cli_id, installed in (old_installed or {}).items():
        if transition["clis"][cli_id]["old_projection"]:
            transition["clis"][cli_id]["old_projection"]["installed"] = installed
    _write_transition(path, transition)


def _delete_transition(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise PathSafetyError(f"production transition is not a regular file: {path}")
    path.unlink()
    fsync_directory(path.parent)


def _execute_transition(
    root: Path,
    config_root: Path,
    transition_path: Path,
    transition: dict[str, Any],
    old_state: dict[str, Any],
    target: TargetPlan,
) -> dict[str, Any]:
    reused_projection, reused_mount = _materialize_plan(target)
    _native_verify(target)
    _advance_transition(transition_path, transition, "TARGET_VERIFIED")
    installable = _installable_clis(target)
    _advance_transition(
        transition_path,
        transition,
        "SWAPPING_PRIVATE_PROJECTION",
        install_status=dict.fromkeys(installable, "removing-old"),
    )

    old_plan = old_state["plan"]
    for cli_id in installable:
        if old_plan is not None and _target_plugin_id(old_plan, cli_id) != _target_plugin_id(
            target, cli_id
        ):
            _remove_target(old_plan, cli_id)
        _install_target(target, cli_id)
    _advance_transition(
        transition_path,
        transition,
        "SWAPPING_PRIVATE_PROJECTION",
        install_status=dict.fromkeys(installable, "target-installed"),
        target_installed=dict.fromkeys(installable, True),
        old_installed=dict.fromkeys(installable, False),
    )

    atomic_replace_json(root / "production" / "active.json", target.active)
    _advance_transition(
        transition_path,
        transition,
        "ACTIVE_POINTER_COMMITTED",
        recovery_action="complete-target",
    )
    _validate_stable_active(
        root,
        config_root,
        target.library_id,
        verify_installation=True,
        executables=target.executables,
    )
    _delete_transition(transition_path)
    return {
        "library_id": target.library_id,
        "production_version_id": target.version_id,
        "previous_production_version_id": old_state["active"]["active_production_version_id"],
        "production_manifest_sha256": target.validated["manifest_sha256"],
        "bundle": str(target.mount_root / "bundle.json"),
        "bundle_sha256": target.bundle_sha256,
        "plugin_name": target.plugin_name,
        "marketplace_name": target.marketplace_name,
        "effective_on": "new-session",
        "reused_projection": reused_projection,
        "reused_mount": reused_mount,
        "idempotent": False,
    }


def _perform_activation_locked(
    root: Path,
    config_root: Path,
    library_id: str,
    version_id: str,
    *,
    replace_transition: bool = False,
) -> dict[str, Any]:
    compatibilities = _detect_target_compatibilities()
    executables = {
        cli_id: compatibility.executable for cli_id, compatibility in compatibilities.items()
    }
    # Objects are verified first without the installation gate.  Re-activating the
    # already-active version must be able to converge a missing or replaced target
    # installation, otherwise a target that lost its plugin outside PAL would have
    # no supported way back to the pointer PAL already considers active.
    old_state = _validate_stable_active(
        root,
        config_root,
        library_id,
        verify_installation=False,
        executables=executables,
    )
    if old_state["active"]["active_production_version_id"] == version_id:
        plan = old_state["plan"]
        if plan is None:  # pragma: no cover - identity comparison excludes this
            raise ProductionError("active production plan is unavailable")
        _materialize_plan(plan)
        for cli_id in _installable_clis(plan):
            _install_target(plan, cli_id)
            _verify_installed(plan, cli_id)
        return {
            "library_id": library_id,
            "production_version_id": version_id,
            "previous_production_version_id": version_id,
            "production_manifest_sha256": plan.validated["manifest_sha256"],
            "bundle": str(plan.mount_root / "bundle.json"),
            "bundle_sha256": plan.bundle_sha256,
            "plugin_name": plan.plugin_name,
            "marketplace_name": plan.marketplace_name,
            "effective_on": "new-session",
            "reused_projection": True,
            "reused_mount": True,
            "idempotent": True,
        }

    # Switching away from another version still requires a stable starting point,
    # so the outgoing installation is verified before any target is touched.
    old_plan = old_state["plan"]
    if old_plan is not None and not replace_transition:
        for cli_id in _installable_clis(old_plan):
            _verify_installed(old_plan, cli_id)

    created_at = utc_now()
    target = _build_target_plan(
        root,
        config_root,
        library_id,
        version_id,
        activation_time=created_at,
        executables=executables,
        compatibilities=compatibilities,
    )
    transition_path = _transition_path(config_root, library_id)
    if not replace_transition and (transition_path.exists() or transition_path.is_symlink()):
        raise ProductionError(f"production transition already exists: {transition_path}")
    transition = _new_transition(
        library_id=library_id,
        old_state=old_state,
        target=target,
        created_at=created_at,
        old_codex_installed=not replace_transition,
    )
    _write_transition(transition_path, transition)
    return _execute_transition(
        root,
        config_root,
        transition_path,
        transition,
        old_state,
        target,
    )


def _load_transition(path: Path, library_id: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PathSafetyError(f"production transition is not a regular file: {path}")
    transition = load_json_object(path)
    validate_instance(TRANSITION_SCHEMA, transition)
    if transition["library_id"] != library_id:
        raise IntegrityError("production transition library identity mismatch")
    return transition


def _verify_snapshot_identity(
    snapshot: dict[str, Any],
    plan: TargetPlan,
    cli_id: str,
) -> None:
    expected = _projection_snapshot(plan, cli_id, installed=snapshot["installed"])
    if snapshot != expected:
        raise IntegrityError(f"production transition {cli_id} projection snapshot drifted")


def _verify_plan_objects(plan: TargetPlan, label: str, *, optional: bool) -> None:
    """Verify an immutable mount/projection pair, optionally allowing absence."""

    for root, files, object_label in (
        (plan.projection_root, plan.projection_files, "projection"),
        (plan.mount_root, plan.mount_files, "mount"),
    ):
        exists = root.exists() or root.is_symlink()
        if not exists and optional:
            continue
        if not exists:
            raise IntegrityError(f"{label} {object_label} is missing: {root}")
        _verify_files(root, files, f"{label} {object_label} {plan.version_id}")


def _verify_transition_objects(
    transition: dict[str, Any],
    target: TargetPlan,
    old: TargetPlan | None,
) -> None:
    """Verify persisted identities and both immutable sides before recovery actions."""

    for cli_id in TARGET_CLIS:
        snapshot = transition["clis"][cli_id]["target_projection"]
        _verify_snapshot_identity(snapshot, target, cli_id)
        old_snapshot = transition["clis"][cli_id]["old_projection"]
        if old is None:
            if old_snapshot is not None:
                raise IntegrityError("production transition has an unexpected old projection")
        elif old_snapshot is None:
            raise IntegrityError("production transition old projection is missing")
        else:
            _verify_snapshot_identity(old_snapshot, old, cli_id)
    _verify_plan_objects(target, "production transition target", optional=True)
    if old is not None:
        _verify_plan_objects(old, "production transition old", optional=False)


def _restore_old_before_commit(
    root: Path,
    config_root: Path,
    library_id: str,
    transition: dict[str, Any],
    target: TargetPlan,
    active: dict[str, Any],
    executables: dict[str, str],
) -> None:
    from_version = transition["from_version_id"]
    if from_version is None:
        if active != _empty_active(library_id):
            raise IntegrityError("transition old active pointer is not the canonical empty pointer")
        for cli_id in _installable_clis(target):
            _remove_target(target, cli_id)
        return
    old_plan = _plan_for_active(root, config_root, library_id, active, executables)
    if old_plan.version_id != from_version:
        raise IntegrityError("transition from-version differs from the active pointer")
    _native_verify(old_plan)
    for cli_id in _installable_clis(target):
        if _target_plugin_id(old_plan, cli_id) != _target_plugin_id(target, cli_id):
            _remove_target(target, cli_id)
        _install_target(old_plan, cli_id)
    _validate_stable_active(
        root,
        config_root,
        library_id,
        verify_installation=True,
        executables=executables,
    )


def _recover_target_after_commit(
    root: Path,
    config_root: Path,
    library_id: str,
    transition_path: Path,
    transition: dict[str, Any],
    target: TargetPlan,
    executables: dict[str, str],
) -> dict[str, Any]:
    try:
        _materialize_plan(target)
        _native_verify(target)
        installable = _installable_clis(target)
        for cli_id in installable:
            _install_target(target, cli_id)
        _advance_transition(
            transition_path,
            transition,
            "ACTIVE_POINTER_COMMITTED",
            recovery_action="complete-target",
            install_status=dict.fromkeys(installable, "target-installed"),
            target_installed=dict.fromkeys(installable, True),
            old_installed=dict.fromkeys(installable, False),
        )
        _validate_stable_active(
            root,
            config_root,
            library_id,
            verify_installation=True,
            executables=executables,
        )
        _delete_transition(transition_path)
        return {
            "library_id": library_id,
            "recovered": True,
            "recovery_action": "completed-target",
            "active_production_version_id": target.version_id,
        }
    except (IntegrityError, PathSafetyError):
        # Object or path drift must remain visible for operator repair.  Do not
        # hide it by guessing another production selection.
        raise
    except (ProductionError, OSError):
        from_version = transition["from_version_id"]
        if from_version is None:
            for cli_id in _installable_clis(target):
                _remove_target(target, cli_id)
            empty = _empty_active(library_id)
            if sha256_bytes(formatted_json_bytes(empty)) != transition["old_active_sha256"]:
                raise IntegrityError(
                    "cannot reconstruct the transition's empty old pointer"
                ) from None
            atomic_replace_json(root / "production" / "active.json", empty)
            _delete_transition(transition_path)
            return {
                "library_id": library_id,
                "recovered": True,
                "recovery_action": "rolled-back-empty",
                "active_production_version_id": None,
            }

        # Replace the failed completion transaction only after the old target
        # has been validated by _perform_activation_locked.  The replacement
        # itself is the normal activation state machine with the old version as
        # its explicit target.
        result = _perform_activation_locked(
            root,
            config_root,
            library_id,
            from_version,
            replace_transition=True,
        )
        return {
            "library_id": library_id,
            "recovered": True,
            "recovery_action": "rolled-back-old",
            "active_production_version_id": result["production_version_id"],
        }


def _recover_locked(
    root: Path,
    config_root: Path,
    library_id: str,
) -> dict[str, Any]:
    path = _transition_path(config_root, library_id)
    if not (path.exists() or path.is_symlink()):
        compatibilities = _detect_target_compatibilities()
        executables = {
            cli_id: compatibility.executable for cli_id, compatibility in compatibilities.items()
        }
        stable = _validate_stable_active(
            root,
            config_root,
            library_id,
            verify_installation=True,
            executables=executables,
        )
        return {
            "library_id": library_id,
            "recovered": False,
            "recovery_action": "none",
            "active_production_version_id": stable["active"]["active_production_version_id"],
        }

    transition = _load_transition(path, library_id)
    compatibilities = _detect_target_compatibilities()
    executables = {
        cli_id: compatibility.executable for cli_id, compatibility in compatibilities.items()
    }
    active, active_sha256 = _read_active(root, library_id)
    target = _build_target_plan(
        root,
        config_root,
        library_id,
        transition["to_version_id"],
        activation_time=transition["created_at"],
        executables=executables,
        compatibilities=compatibilities,
    )
    if target.active_sha256 != transition["new_active_sha256"]:
        raise IntegrityError("production transition target active digest is not reproducible")
    old_plan = None
    if transition["from_version_id"] is not None:
        old_plan = _build_target_plan(
            root,
            config_root,
            library_id,
            transition["from_version_id"],
            activation_time=transition["created_at"],
            executables=executables,
            compatibilities=compatibilities,
        )
    _verify_transition_objects(transition, target, old_plan)

    if active_sha256 == transition["old_active_sha256"]:
        _restore_old_before_commit(
            root,
            config_root,
            library_id,
            transition,
            target,
            active,
            executables,
        )
        _delete_transition(path)
        return {
            "library_id": library_id,
            "recovered": True,
            "recovery_action": "restored-old",
            "active_production_version_id": transition["from_version_id"],
        }
    if active_sha256 == transition["new_active_sha256"]:
        if active != target.active:
            raise IntegrityError("target active digest matched but pointer content differed")
        return _recover_target_after_commit(
            root,
            config_root,
            library_id,
            path,
            transition,
            target,
            executables,
        )
    raise IntegrityError("active pointer matches neither side of the production transition")


def _activate_or_recover(
    library_root: Path,
    version_id: str,
    *,
    config_root: Path | None,
) -> dict[str, Any]:
    root = canonical_existing_root(library_root)
    context = doctor_development_context(root)
    library_id = context["library_id"]
    pal_config_root = resolve_config_root(config_root, create=True)
    with _activation_lock(pal_config_root, library_id):
        if not (root / "production/versions" / version_id).is_dir():
            raise ProductionError("生产内容仅存于旧证据归档或已缺失；请先正常发布，再同步到 CLI")
        validate_production_version(root, version_id)
        path = _transition_path(pal_config_root, library_id)
        if path.exists() or path.is_symlink():
            _recover_locked(root, pal_config_root, library_id)
        doctor_library(root)
        try:
            return _perform_activation_locked(
                root,
                pal_config_root,
                library_id,
                version_id,
            )
        except (IntegrityError, PathSafetyError):
            # Drift discovered after a durable transition is deliberately left
            # for explicit recovery; pre-transition drift leaves no record.
            raise
        except (ProductionError, OSError) as exc:
            transition = _transition_path(pal_config_root, library_id)
            if transition.exists() or transition.is_symlink():
                _recover_locked(root, pal_config_root, library_id)
                raise ProductionError(
                    f"production activation failed and was recovered: {exc}"
                ) from exc
            raise


@lifecycle_write
def activate_production(
    library_root: Path,
    version_id: str,
    *,
    config_root: Path | None = None,
) -> dict[str, Any]:
    """Activate one immutable production version for new Claude/Codex sessions."""

    return _activate_or_recover(library_root, version_id, config_root=config_root)


@lifecycle_write
def recover_production(
    library_root: Path,
    *,
    config_root: Path | None = None,
) -> dict[str, Any]:
    """Converge one durable production transition without guessing state."""

    root = canonical_existing_root(library_root)
    context = doctor_development_context(root)
    pal_config_root = resolve_config_root(config_root, create=False)
    with _activation_lock(pal_config_root, context["library_id"]):
        return _recover_locked(root, pal_config_root, context["library_id"])


def _runtime_candidates(
    plan: TargetPlan,
    cli_id: str,
    runtime_plugin_root: Path,
) -> list[dict[str, Any]]:
    production = plan.validated["production"]
    releases_by_id = {item["release_id"]: item for item in plan.validated["releases"]}
    source_artifacts = {
        item["artifact"]["artifact_id"]: item["artifact"]
        for release in plan.validated["releases"]
        for item in release["artifacts"]
    }
    plugin_root = _plugin_root(plan, cli_id)
    candidates: list[dict[str, Any]] = []
    for artifact in production["artifacts"]:
        if cli_id not in artifact["covered_clis"]:
            continue
        release = releases_by_id.get(artifact["release_id"])
        source = source_artifacts.get(artifact["artifact_id"])
        if release is None or source is None:
            raise IntegrityError(
                f"runtime artifact has no release source: {artifact['artifact_id']}"
            )
        driver = component_type_driver(source["kind"])
        if driver.type_id != SKILL_COMPONENT_TYPE_ID:
            raise ProductionError(
                f"v1 runtime only supports registered Skill artifacts: {artifact['artifact_id']}"
            )
        payload_root = require_inside(
            plan.validated["manifest_path"].parent,
            artifact["payload_root"],
            f"runtime artifact {artifact['artifact_id']}",
        )
        resolved = driver.runtime_candidates(
            artifact=source,
            production_artifact=artifact,
            unit_id=release["unit_id"],
            payload_root=payload_root,
            projected_root=plugin_root,
            runtime_root=runtime_plugin_root,
        )
        if not resolved:
            raise ProductionError(
                f"artifact driver found no runtime object: {artifact['artifact_id']}"
            )
        expected_identity = {
            "unit_id": release["unit_id"],
            "release_id": artifact["release_id"],
            "artifact_id": artifact["artifact_id"],
            "profile_id": artifact["profile_id"],
            "covered_clis": artifact["covered_clis"],
            "tree_sha256": artifact["tree_sha256"],
        }
        for candidate in resolved:
            if any(candidate.get(field) != value for field, value in expected_identity.items()):
                raise IntegrityError(
                    f"artifact driver returned a wrong runtime identity: {artifact['artifact_id']}"
                )
        candidates.extend(resolved)
    if not candidates:
        raise ProductionError(f"active production has no runtime Skill for {cli_id}")
    names = [item["skill_name"] for item in candidates]
    if len(names) != len(set(names)):
        raise ProductionError(f"active production has duplicate Skill names for {cli_id}")
    return candidates


@contextmanager
def active_runtime_context(
    library_root: Path,
    cli_id: str,
    *,
    config_root: Path | None = None,
) -> Iterator[dict[str, Any]]:
    """Hold the activation lock while yielding one validated P2 runtime context."""

    if cli_id not in TARGET_CLIS:
        raise ProductionError(f"unsupported runtime CLI: {cli_id}")
    root = canonical_existing_root(library_root)
    context = doctor_development_context(root)
    pal_config_root = resolve_config_root(config_root, create=False)
    with _activation_lock(pal_config_root, context["library_id"]):
        require_no_cleanup(root)
        transition = pal_config_root / f"transitions/production/{context['library_id']}.json"
        if transition.exists() or transition.is_symlink():
            raise ProductionError(
                f"cannot start a production run during a transition: {transition}"
            )
        compatibility = require_compatible_cli(cli_id)
        executable = compatibility.executable
        active, active_sha256 = _read_active(root, context["library_id"])
        if active["active_production_version_id"] is None:
            raise ProductionError("cannot start a production run without an active version")
        executables = {target: "" for target in TARGET_CLIS}
        executables[cli_id] = executable
        plan = _plan_for_active(
            root,
            pal_config_root,
            context["library_id"],
            active,
            executables,
        )
        validate_production_mount(root, plan.version_id, config_root=pal_config_root)
        _verify_files(plan.projection_root, plan.projection_files, "runtime production projection")
        source_plugin_root = _plugin_root(plan, cli_id)
        if cli_id == "claude-code":
            _run_claude_validation(plan)
            installation = _verify_claude_installed(plan)
            runtime_plugin_root = _claude_installed_plugin_root(plan, installation)
        else:
            installation = _verify_codex_installed(plan)
            runtime_plugin_root = _codex_cached_plugin_root(plan, installation)
        record_path = plan.mount_root / f"{cli_id}.json"
        record = load_json_object(record_path)
        yield {
            "library_root": root,
            "config_root": pal_config_root,
            "library_id": context["library_id"],
            "cli_id": cli_id,
            "cli_version": compatibility.actual_version,
            "cli_compatibility": compatibility.diagnostic(),
            "executable": executable,
            "production_version_id": plan.version_id,
            "production_manifest_sha256": plan.validated["manifest_sha256"],
            "active_sha256": active_sha256,
            "mount_bundle_path": plan.mount_root / "bundle.json",
            "mount_bundle_sha256": plan.bundle_sha256,
            "cli_mount_path": record_path,
            "cli_mount_sha256": sha256_file(record_path),
            "cli_mount": record,
            "projection_root": plan.projection_root / cli_id,
            "projection_tree_sha256": plan.cli_projection_digests[cli_id],
            "plugin_name": plan.plugin_name,
            "marketplace_name": plan.marketplace_name if cli_id == "codex" else None,
            "plugin_root": source_plugin_root,
            "runtime_plugin_root": runtime_plugin_root,
            "skills": _runtime_candidates(plan, cli_id, runtime_plugin_root),
        }


def launch_production_entry(
    library_root: Path,
    cli_id: str,
    *,
    config_root: Path | None = None,
    cli_arguments: Sequence[str] = (),
) -> int:
    """Launch an interactive target CLI with the active production projection."""

    with active_runtime_context(library_root, cli_id, config_root=config_root) as runtime:
        environment = os.environ.copy()
        environment["PAL_LIBRARY_ROOT"] = str(runtime["library_root"])
        environment["PAL_PRODUCTION_VERSION_ID"] = runtime["production_version_id"]
        if config_root is not None:
            environment["PAL_CONFIG_ROOT"] = str(runtime["config_root"])
        # Both targets now resolve the active production plugin from their own
        # persistent installation, so no session-scoped injection is needed.
        command = [runtime["executable"], *cli_arguments]
        return subprocess.call(command, env=environment)


__all__ = [
    "active_runtime_context",
    "activate_production",
    "launch_production_entry",
    "recover_production",
    "validate_production_mount",
]
