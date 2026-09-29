"""Native platform behavior and old library compatibility (ACC-001/011/012)."""

import json
import os
import subprocess
import sys

import pytest

from pal import locking
from pal.errors import IntegrityError, PathSafetyError
from pal.io import (
    atomic_replace_json,
    formatted_json_bytes,
    sha256_file,
    tree_digest,
    write_new_bytes,
)
from pal.library import doctor_library, initialize_library
from pal.paths import canonical_init_target, normalize_relative_path
from pal.platform_support import process_exists
from pal.schema_catalog import SCHEMA_CATALOG


def test_locks_coordinate_processes_and_release(tmp_path):
    path = tmp_path / "shared.lock"
    code = """
import sys
from pal import locking
with open(sys.argv[1], 'a+b') as f:
    try:
        locking.flock(f, int(sys.argv[2]) | locking.LOCK_NB)
    except BlockingIOError:
        sys.exit(7)
    locking.flock(f, locking.LOCK_UN)
"""

    def acquire(mode):
        return subprocess.run(
            [sys.executable, "-c", code, str(path), str(mode)], timeout=20
        ).returncode

    with path.open("a+b") as handle:
        locking.flock(handle, locking.LOCK_SH | locking.LOCK_NB)
        assert acquire(locking.LOCK_SH) == 0
        assert acquire(locking.LOCK_EX) == 7
        locking.flock(handle, locking.LOCK_UN)
        locking.flock(handle, locking.LOCK_EX | locking.LOCK_NB)
        assert acquire(locking.LOCK_SH) == 7
        assert acquire(locking.LOCK_EX) == 7
    assert acquire(locking.LOCK_EX) == 0  # process/handle exit releases the lock


def test_persistent_bytes_and_empty_directory_initialization(tmp_path):
    root = tmp_path / "中文 library"
    root.mkdir()
    initialize_library(root, "native-library")
    doctor_library(root)
    path = tmp_path / "字节.json"
    value = {"说明": "第一行\n第二行"}
    write_new_bytes(path, formatted_json_bytes(value))
    assert path.read_bytes() == formatted_json_bytes(value)
    atomic_replace_json(path, {"更新": True})
    assert path.read_bytes() == formatted_json_bytes({"更新": True})


def test_old_sealed_catalog_remains_readable_and_unmodified(tmp_path):
    root = tmp_path / "old-library"
    initialize_library(root, "legacy")
    registry_path = root / "schemas/v1/index.json"
    registry = json.loads(registry_path.read_bytes())
    registry["generated_by_pal_version"] = "0.2.4"
    for entry in registry["schemas"]:
        schema = SCHEMA_CATALOG[entry["name"]]
        path = root / entry["path"]
        path.write_bytes(formatted_json_bytes(schema))
        entry["$id"] = schema["$id"]
        entry["sha256"] = sha256_file(path)
    registry_path.write_bytes(formatted_json_bytes(registry))
    manifest_path = root / "library.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["schema_registry"]["sha256"] = sha256_file(registry_path)
    manifest_path.write_bytes(formatted_json_bytes(manifest))
    before = tree_digest(root)
    doctor_library(root)
    assert tree_digest(root) == before
    # Even rehashed changes must not become trusted catalog content.
    schema_path = root / registry["schemas"][0]["path"]
    schema = json.loads(schema_path.read_bytes())
    schema["title"] = "untrusted"
    schema_path.write_bytes(formatted_json_bytes(schema))
    registry["schemas"][0]["sha256"] = sha256_file(schema_path)
    registry_path.write_bytes(formatted_json_bytes(registry))
    manifest["schema_registry"]["sha256"] = sha256_file(registry_path)
    manifest_path.write_bytes(formatted_json_bytes(manifest))
    with pytest.raises(IntegrityError, match="catalog"):
        doctor_library(root)


def test_process_inspection_does_not_terminate_child():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert process_exists(child.pid)
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert not process_exists(child.pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows filesystem aliases")
@pytest.mark.parametrize("name", ["NUL", "aux.txt", "COM1", "a:b", "a.", "a "])
def test_windows_rejects_ambiguous_names(tmp_path, name):
    with pytest.raises(PathSafetyError):
        normalize_relative_path(name, "artifact")
    with pytest.raises(PathSafetyError):
        canonical_init_target(tmp_path / name)


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse points")
def test_windows_rejects_junction_ancestor(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "junction"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True
    )
    try:
        with pytest.raises(PathSafetyError, match="reparse"):
            canonical_init_target(link / "library")
    finally:
        link.rmdir()
