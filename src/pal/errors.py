"""Expected fail-closed PAL errors."""

from __future__ import annotations


class PALError(Exception):
    """An expected validation, integrity, or lifecycle rejection."""


class PathSafetyError(PALError):
    """A path violates an PAL root or symbolic-link boundary."""


class SchemaValidationError(PALError):
    """A formal PAL schema or instance is invalid."""


class IntegrityError(PALError):
    """A content digest or immutable reference does not match."""


class InitializationError(PALError):
    """A target cannot be initialized as a new PAL library."""


class CreationError(PALError):
    """A P1 creation transaction violates its state or protocol contract."""


class CompatibilityError(PALError):
    """A target CLI fails the minimum-version, capability, or behavior gate."""


class ReleaseError(PALError):
    """A logical-unit release violates its immutable snapshot contract."""


class ProductionError(PALError):
    """A production aggregate violates its release or artifact closure."""


class UsageError(PALError):
    """A controlled P2 run or usage record violates its evidence contract."""


class QuickstartError(PALError):
    """The guided P0-P2 workflow stopped at a recoverable stage."""

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        completed_stages: tuple[str, ...],
        next_action: str,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.completed_stages = completed_stages
        self.next_action = next_action


class QuickstartCancelled(PALError):
    """The user left the wizard before any lifecycle operation started."""
