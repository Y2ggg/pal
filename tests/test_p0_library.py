"""P0 initialization, boundaries, and fail-closed acceptance tests.

Traceability: PRD-P0-001 through PRD-P0-005, PRD-TECH-001; ACC-001, ACC-012.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import pal.library as library_module
from pal.errors import InitializationError
from pal.io import sha256_file, tree_digest
from pal.library import doctor_library, initialize_library
from pal.schema_catalog import SCHEMA_FILENAMES


def run_pal(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pal", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def parse_output(process: subprocess.CompletedProcess[str], stream: str) -> dict[str, object]:
    raw = process.stdout if stream == "stdout" else process.stderr
    return json.loads(raw)


def test_e2e_p0_01_acc_001_initializes_complete_empty_library(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    library_root.mkdir()

    initialized = run_pal(
        "init",
        "--library",
        str(library_root),
        "--library-id",
        "acceptance-library",
    )

    assert initialized.returncode == 0, initialized.stderr
    output = parse_output(initialized, "stdout")
    assert output["proof"] == "PAL_LIBRARY_INITIALIZED"
    assert output["schema_count"] == 14

    manifest = json.loads((library_root / "library.json").read_text(encoding="utf-8"))
    active = json.loads((library_root / "production" / "active.json").read_text(encoding="utf-8"))
    registry = json.loads(
        (library_root / "schemas" / "v1" / "index.json").read_text(encoding="utf-8")
    )

    assert manifest["library_id"] == "acceptance-library"
    assert manifest["target_clis"] == ["claude-code", "codex"]
    assert set(manifest["specifications"]["cli"]) == {"claude-code", "codex"}
    assert len(manifest["capability_profiles"]) == 2
    assert tuple(entry["name"] for entry in registry["schemas"]) == SCHEMA_FILENAMES
    assert active == {
        "schema_version": 1,
        "library_id": "acceptance-library",
        "active_production_version_id": None,
        "production_manifest": None,
        "production_mount_bundle": None,
        "activated_at": None,
    }

    assert not any((library_root / "development" / "units").iterdir())
    assert not any((library_root / "releases" / "units").iterdir())
    assert not any((library_root / "production" / "versions").iterdir())
    assert not any((library_root / "records" / "usage").iterdir())

    healthy = run_pal("doctor", "--library", str(library_root))
    assert healthy.returncode == 0, healthy.stderr
    assert parse_output(healthy, "stdout")["proof"] == "PAL_LIBRARY_HEALTHY"


def test_prd_p0_002_acc_001_development_change_does_not_touch_production(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    initialize_library(library_root, "boundary-library")
    production_before = tree_digest(library_root / "production")
    active_before = sha256_file(library_root / "production" / "active.json")

    draft = library_root / "development" / "units" / "local-draft.txt"
    draft.write_text("development-only\n", encoding="utf-8")

    assert tree_digest(library_root / "production") == production_before
    assert sha256_file(library_root / "production" / "active.json") == active_before
    assert doctor_library(library_root)["active_production_version_id"] is None


def test_prd_p0_001_acc_012_nonempty_target_is_preserved_and_rejected(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    library_root.mkdir()
    sentinel = library_root / "owned-by-user.txt"
    sentinel.write_text("keep\n", encoding="utf-8")

    process = run_pal(
        "init",
        "--library",
        str(library_root),
        "--library-id",
        "nonempty-library",
    )

    assert process.returncode == 20
    assert parse_output(process, "stderr")["proof"] == "PAL_LIBRARY_INIT_REJECTED"
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert not (library_root / "library.json").exists()


def test_prd_p0_001_acc_012_invalid_id_leaves_no_partial_library(tmp_path: Path) -> None:
    library_root = tmp_path / "library"

    process = run_pal(
        "init",
        "--library",
        str(library_root),
        "--library-id",
        "INVALID ID",
    )

    assert process.returncode == 20
    assert not library_root.exists()
    assert not list(tmp_path.glob(".pal-init-*.tmp"))


def test_prd_tech_001_acc_012_unvalidated_platform_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(library_module, "fcntl", None)
    with pytest.raises(InitializationError, match="macOS/POSIX"):
        doctor_library(Path("unused"))


def test_prd_p0_001_acc_012_build_failure_leaves_no_partial_library(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library_root = tmp_path / "library"

    def fail_build(_root: Path, _library_id: str) -> None:
        raise InitializationError("injected build failure")

    monkeypatch.setattr(library_module, "_build_library_tree", fail_build)
    with pytest.raises(InitializationError, match="injected build failure"):
        initialize_library(library_root, "failure-library")

    assert not library_root.exists()
    assert not list(tmp_path.glob(".pal-init-*.tmp"))


def test_prd_p0_003_acc_012_doctor_rejects_manifest_unknown_field(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    initialize_library(library_root, "unknown-field-library")
    manifest_path = library_root / "library.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["rating"] = 5
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    process = run_pal("doctor", "--library", str(library_root))

    assert process.returncode == 20
    assert parse_output(process, "stderr")["proof"] == "PAL_LIBRARY_DOCTOR_REJECTED"


def test_prd_p0_003_acc_012_doctor_rejects_reference_digest_drift(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    initialize_library(library_root, "digest-library")
    active_path = library_root / "production" / "active.json"
    active_before = sha256_file(active_path)
    common_spec = library_root / "development" / "specifications" / "common" / "skill-v1.md"
    common_spec.write_text(common_spec.read_text(encoding="utf-8") + "drift\n", encoding="utf-8")

    process = run_pal("doctor", "--library", str(library_root))

    assert process.returncode == 20
    assert "SHA-256 mismatch" in str(parse_output(process, "stderr")["error"])
    assert sha256_file(active_path) == active_before


def test_prd_p0_003_acc_012_doctor_rejects_reference_symlink(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    initialize_library(library_root, "symlink-library")
    common_spec = library_root / "development" / "specifications" / "common" / "skill-v1.md"
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    common_spec.unlink()
    common_spec.symlink_to(outside)

    process = run_pal("doctor", "--library", str(library_root))

    assert process.returncode == 20
    assert "symbolic link" in str(parse_output(process, "stderr")["error"])


def test_prd_spec_002_acc_012_doctor_rejects_rehashed_unvalidated_profile(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    initialize_library(library_root, "profile-library")
    profile_path = (
        library_root
        / "development"
        / "specifications"
        / "common"
        / "skill-md-v1-basic.profile.json"
    )
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["captured_at"] = "2026-08-15T00:00:00Z"
    profile_path.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path = library_root / "library.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["capability_profiles"][0]["sha256"] = sha256_file(profile_path)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    process = run_pal("doctor", "--library", str(library_root))

    assert process.returncode == 20
    assert "not validated by PAL 0.1.0" in str(parse_output(process, "stderr")["error"])
