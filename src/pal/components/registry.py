"""Fail-closed registry for trusted PAL plugin-component contracts."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..errors import IntegrityError
from .base import ComponentTypeDriver


class ComponentTypeRegistry:
    """Register trusted drivers uniquely by component type and contract ID."""

    def __init__(self, drivers: Iterable[ComponentTypeDriver] = ()) -> None:
        self._by_type: dict[str, ComponentTypeDriver] = {}
        self._by_contract: dict[str, ComponentTypeDriver] = {}
        for driver in drivers:
            self.register(driver)

    def register(self, driver: ComponentTypeDriver) -> None:
        if not driver.type_id or not driver.contract_id:
            raise ValueError("component type and contract IDs must be non-empty")
        if driver.type_id in self._by_type:
            raise ValueError(f"component type driver is already registered: {driver.type_id}")
        if driver.contract_id in self._by_contract:
            raise ValueError(f"component contract is already registered: {driver.contract_id}")
        self._by_type[driver.type_id] = driver
        self._by_contract[driver.contract_id] = driver

    def require_type(self, type_id: object) -> ComponentTypeDriver:
        if not isinstance(type_id, str) or not type_id:
            raise IntegrityError("component type must be a non-empty registered ID")
        driver = self._by_type.get(type_id)
        if driver is None:
            raise IntegrityError(f"component type is not registered by this PAL: {type_id}")
        return driver

    def require_kind(self, kind_id: object) -> ComponentTypeDriver:
        """PAL format-v1 compatibility spelling for :meth:`require_type`."""

        return self.require_type(kind_id)

    def require_contract(self, contract_id: object) -> ComponentTypeDriver:
        if not isinstance(contract_id, str) or not contract_id:
            raise IntegrityError("artifact contract must be a non-empty registered ID")
        driver = self._by_contract.get(contract_id)
        if driver is None:
            raise IntegrityError(f"component contract is not registered by this PAL: {contract_id}")
        return driver

    def require_profile(self, profile: dict[str, Any]) -> ComponentTypeDriver:
        driver = self.require_type(profile.get("artifact_kind"))
        driver.validate_profile(profile)
        return driver

    def contracts(self) -> tuple[dict[str, str], ...]:
        return tuple(
            {"type_id": kind_id, "contract_id": driver.contract_id}
            for kind_id, driver in sorted(self._by_type.items())
        )


__all__ = ["ComponentTypeRegistry"]
