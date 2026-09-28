"""Interfaces and normalized values for PAL plugin-component contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PayloadFile:
    """One validated file to materialize below an artifact payload root."""

    path: str
    sha256: str
    material: bytes


@dataclass(frozen=True)
class ValidatedCandidate:
    """A kind driver result that the lifecycle core can commit generically."""

    files: tuple[PayloadFile, ...]
    dependencies: tuple[dict[str, str], ...] = ()


class ComponentTypeDriver(ABC):
    """Contract implemented by one trusted, versioned Plugin component type.

    Persisted PAL format v1 still calls the component discriminator ``kind``.
    That spelling belongs to the v1 codec; lifecycle code depends on this
    component contract and does not treat a target CLI plugin as the fact source.
    """

    type_id: str
    contract_id: str

    @property
    def kind_id(self) -> str:
        """PAL format-v1 compatibility spelling for ``type_id``."""

        return self.type_id

    @abstractmethod
    def accepts_unit_id(self, unit_id: str) -> bool:
        """Return whether the logical-unit ID satisfies this kind's v1 naming contract."""

    @abstractmethod
    def validate_profile(self, profile: dict[str, Any]) -> None:
        """Validate kind-specific semantics after the formal profile schema passes."""

    @abstractmethod
    def plan_candidate(self, unit_id: str, artifact_id: str) -> dict[str, Any]:
        """Return deterministic v1 plan fields for one candidate artifact."""

    @abstractmethod
    def candidate_paths(self, artifact_plan: dict[str, Any]) -> tuple[str, ...]:
        """Return required candidate files relative to the transaction root."""

    def candidate_tree_roots(self, artifact_plan: dict[str, Any]) -> tuple[str, ...]:
        """Return directories allowed to contain additional candidate files."""

        return ()

    @abstractmethod
    def public_candidate_paths(
        self,
        transaction_root: Path,
        artifact_plan: dict[str, Any],
    ) -> dict[str, str]:
        """Return driver-specific paths exposed by ``create begin``."""

    @abstractmethod
    def validate_candidate(
        self,
        transaction_root: Path,
        artifact_plan: dict[str, Any],
        unit_id: str,
        profile: dict[str, Any],
    ) -> ValidatedCandidate:
        """Validate staged semantic content and normalize its artifact payload."""

    @abstractmethod
    def validate_payload(
        self,
        artifact: dict[str, Any],
        payload_root: Path,
    ) -> None:
        """Validate kind semantics for an already materialized immutable payload."""

    @abstractmethod
    def projection_files(
        self,
        artifact: dict[str, Any],
        payload_root: Path,
    ) -> dict[str, bytes]:
        """Return canonical payload files to place in a target CLI projection."""

    @abstractmethod
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
        """Resolve and verify actual runtime objects for controlled consumption."""

    @abstractmethod
    def inspect_usage_events(
        self,
        cli_id: str,
        events: list[dict[str, Any]],
        context: dict[str, Any],
        *,
        verify_runtime: bool = True,
    ) -> Any:
        """Inspect target CLI events using this kind's actual-consumption contract."""


__all__ = ["ComponentTypeDriver", "PayloadFile", "ValidatedCandidate"]
