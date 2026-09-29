"""Fail-closed target admission by version floor, probes, and blacklist."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import CompatibilityError
from .platform_support import run_external
from .targets import target_driver

VERSION_PATTERN = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)")


@dataclass(frozen=True)
class ProbeResult:
    probe_id: str
    detail: str


@dataclass(frozen=True)
class CliCompatibility:
    cli_id: str
    executable: str
    actual_version: str
    minimum_version: str
    classification: str
    probes: tuple[ProbeResult, ...]

    def diagnostic(self) -> dict[str, Any]:
        return {
            "cli_id": self.cli_id,
            "executable": self.executable,
            "actual_version": self.actual_version,
            "minimum_version": self.minimum_version,
            "classification": self.classification,
            "probes": [
                {"probe_id": probe.probe_id, "detail": probe.detail} for probe in self.probes
            ],
        }


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = VERSION_PATTERN.fullmatch(value)
    if match is None:
        raise CompatibilityError(f"invalid semantic CLI version: {value}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _run(
    arguments: list[str],
    *,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return run_external(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CompatibilityError(f"cannot execute compatibility probe: {arguments[0]}") from exc


def _checked_json_command(
    arguments: list[str],
    *,
    environment: dict[str, str],
) -> dict[str, Any]:
    process = _run(arguments, environment=environment)
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or f"exit {process.returncode}"
        raise CompatibilityError(f"isolated Codex behavior probe failed: {detail}")
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise CompatibilityError("isolated Codex behavior probe returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise CompatibilityError("isolated Codex behavior probe returned a non-object envelope")
    return payload


def _isolated_codex_behavior_probe(executable: str) -> str:
    """Exercise the preferred portable plugin contract under a temporary CODEX_HOME."""

    marketplace_name = "pal-compatibility-probe"
    plugin_name = "pal-compatibility-skill"
    with tempfile.TemporaryDirectory(prefix="pal-codex-compat-") as temporary:
        root = Path(temporary)
        codex_home = root / "codex-home"
        marketplace = root / "marketplace"
        plugin = marketplace / "plugins" / plugin_name
        skill = plugin / "skills" / "pal-compatibility-probe"
        codex_home.mkdir()
        (marketplace / ".agents/plugins").mkdir(parents=True)
        skill.mkdir(parents=True)
        (marketplace / ".agents/plugins/marketplace.json").write_text(
            json.dumps(
                {
                    "name": marketplace_name,
                    "interface": {"displayName": "PAL compatibility probe"},
                    "plugins": [
                        {
                            "name": plugin_name,
                            "source": {
                                "source": "local",
                                "path": f"./plugins/{plugin_name}",
                            },
                            "policy": {
                                "installation": "AVAILABLE",
                                "authentication": "ON_INSTALL",
                            },
                            "category": "Productivity",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (plugin / "plugin.json").write_text(
            json.dumps(
                {
                    "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                    "name": plugin_name,
                    "version": "0.0.0+probe",
                    "description": "PAL isolated compatibility probe",
                }
            ),
            encoding="utf-8",
        )
        (skill / "SKILL.md").write_text(
            "---\nname: pal-compatibility-probe\n"
            "description: Validate an isolated Codex plugin contract.\n"
            "---\n\n# Probe\n\nReturn OK.\n",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(codex_home)
        _checked_json_command(
            [executable, "plugin", "marketplace", "add", str(marketplace), "--json"],
            environment=environment,
        )
        selector = f"{plugin_name}@{marketplace_name}"
        _checked_json_command(
            [executable, "plugin", "add", selector, "--json"],
            environment=environment,
        )
        listed = _checked_json_command(
            [executable, "plugin", "list", "--json"],
            environment=environment,
        )
        installed = listed.get("installed")
        if not isinstance(installed, list) or not any(
            isinstance(item, dict)
            and item.get("pluginId") == selector
            and item.get("installed") is True
            for item in installed
        ):
            raise CompatibilityError("isolated Codex plugin was not observable after installation")
        _checked_json_command(
            [executable, "plugin", "remove", selector, "--json"],
            environment=environment,
        )
        _checked_json_command(
            [
                executable,
                "plugin",
                "marketplace",
                "remove",
                marketplace_name,
                "--json",
            ],
            environment=environment,
        )
    return "temporary CODEX_HOME portable plugin.json add/list/remove passed"


def detect_cli_compatibility(cli_id: str) -> CliCompatibility:
    """Admit one installed target only after every stable-contract probe passes."""

    driver = target_driver(cli_id)
    version_process = _run([driver.executable_name, "--version"])
    if version_process.returncode != 0:
        detail = version_process.stderr.strip() or version_process.stdout.strip()
        raise CompatibilityError(f"cannot read {cli_id} version: {detail}")
    output = f"{version_process.stdout}\n{version_process.stderr}"
    match = VERSION_PATTERN.search(output)
    if match is None:
        raise CompatibilityError(f"cannot parse {cli_id} version: {output.strip()}")
    actual = ".".join(match.groups())
    if actual in driver.incompatible_versions:
        raise CompatibilityError(f"known incompatible {cli_id} version: {actual}")
    if _version_tuple(actual) < _version_tuple(driver.minimum_version):
        raise CompatibilityError(
            f"unsupported {cli_id} version: minimum {driver.minimum_version}, got {actual}"
        )

    results: list[ProbeResult] = []
    for probe in driver.compatibility_probes():
        process = _run([driver.executable_name, *probe.arguments])
        detail = driver.validate_probe_output(
            probe,
            returncode=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
        )
        results.append(ProbeResult(probe.probe_id, detail))
    if cli_id == "codex":
        results.append(
            ProbeResult(
                "isolated-plugin-lifecycle",
                _isolated_codex_behavior_probe(driver.executable_name),
            )
        )
    classification = (
        "verified-version" if actual in driver.verified_versions else "probe-compatible-version"
    )
    return CliCompatibility(
        cli_id=cli_id,
        executable=driver.executable_name,
        actual_version=actual,
        minimum_version=driver.minimum_version,
        classification=classification,
        probes=tuple(results),
    )


__all__ = ["CliCompatibility", "ProbeResult", "detect_cli_compatibility"]
