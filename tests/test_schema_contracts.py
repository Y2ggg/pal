"""Formal schema contract tests for the P0 gate.

Traceability: PRD-P0-003, PRD-TECH-001; ACC-001, ACC-012.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from pal.errors import IntegrityError, PathSafetyError, SchemaValidationError
from pal.io import formatted_json_bytes
from pal.library import document_digest, validate_schema_document
from pal.schema_catalog import (
    SCHEMA_CATALOG,
    SCHEMA_FILENAMES,
    check_catalog,
    validate_instance,
)
from tests.schema_samples import schema_samples


def test_prd_tech_001_acc_001_catalog_has_exactly_14_valid_schemas() -> None:
    check_catalog()
    assert tuple(SCHEMA_CATALOG) == SCHEMA_FILENAMES
    assert len(SCHEMA_FILENAMES) == 14
    assert len({schema["$id"] for schema in SCHEMA_CATALOG.values()}) == 14


@pytest.mark.parametrize("schema_filename", SCHEMA_FILENAMES)
def test_prd_p0_003_acc_001_each_schema_accepts_its_positive_example(
    schema_filename: str,
) -> None:
    validate_instance(schema_filename, schema_samples()[schema_filename])


@pytest.mark.parametrize("schema_filename", SCHEMA_FILENAMES)
def test_prd_p0_003_acc_012_each_schema_rejects_a_missing_field(
    schema_filename: str,
) -> None:
    document = schema_samples()[schema_filename]
    del document[SCHEMA_CATALOG[schema_filename]["required"][0]]
    with pytest.raises(SchemaValidationError):
        validate_instance(schema_filename, document)


@pytest.mark.parametrize("schema_filename", SCHEMA_FILENAMES)
def test_prd_p0_003_acc_012_each_schema_rejects_an_unknown_field(
    schema_filename: str,
) -> None:
    document = schema_samples()[schema_filename]
    document["unexpected_p3_field"] = "forbidden"
    with pytest.raises(SchemaValidationError):
        validate_instance(schema_filename, document)


@pytest.mark.parametrize("schema_filename", SCHEMA_FILENAMES)
def test_prd_p0_003_acc_012_each_schema_rejects_an_unsupported_version(
    schema_filename: str,
) -> None:
    document = schema_samples()[schema_filename]
    document["schema_version"] = 99
    with pytest.raises(SchemaValidationError):
        validate_instance(schema_filename, document)


@pytest.mark.parametrize("schema_filename", SCHEMA_FILENAMES)
def test_prd_p0_003_acc_012_each_schema_rejects_document_path_escape(
    tmp_path: Path,
    schema_filename: str,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    outside = tmp_path / f"outside-{schema_filename}"
    document = schema_samples()[schema_filename]
    outside.write_bytes(formatted_json_bytes(document))
    with pytest.raises(PathSafetyError):
        validate_schema_document(
            root,
            f"../{outside.name}",
            schema_filename,
            document_digest(document),
        )


@pytest.mark.parametrize("schema_filename", SCHEMA_FILENAMES)
def test_prd_p0_003_acc_012_each_schema_rejects_document_digest_mismatch(
    tmp_path: Path,
    schema_filename: str,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    document = deepcopy(schema_samples()[schema_filename])
    relative = f"objects/{schema_filename}"
    path = root / relative
    path.parent.mkdir()
    path.write_bytes(formatted_json_bytes(document))
    with pytest.raises(IntegrityError):
        validate_schema_document(root, relative, schema_filename, "f" * 64)
