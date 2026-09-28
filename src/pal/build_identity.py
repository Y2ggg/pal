"""Frozen process build identity for code and dependencies (WEB-001; ACC-012)."""

import hashlib
from importlib.metadata import version
from pathlib import Path


def package_build_id(package: Path) -> str:
    """Stable across installation paths; change on any backend or dependency update."""
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        name = path.relative_to(package).as_posix().encode()
        content = path.read_bytes()
        for value in (name, content):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    for dependency in ("jsonschema", "markdown-it-py"):
        digest.update(f"{dependency}={version(dependency)}\n".encode())
    return digest.hexdigest()[:20]


# Capture once. Replacing files on disk must not relabel an already-running process.
BUILD_ID = package_build_id(Path(__file__).parent)
