"""Base entities for PatchMon."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PatchMonCoordinator


class PatchMonHostEntity(CoordinatorEntity[PatchMonCoordinator]):
    """Base class for an entity belonging to one PatchMon host."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PatchMonCoordinator, host_id: str) -> None:
        super().__init__(coordinator)
        self.host_id = host_id

    @property
    def host(self) -> dict[str, Any]:
        """Return current host data, or an empty mapping while unavailable."""
        return self.coordinator.host(self.host_id) or {}

    @property
    def available(self) -> bool:
        """Return whether this host is present in the latest inventory."""
        return super().available and self.coordinator.host(self.host_id) is not None

    @property
    def device_info(self) -> DeviceInfo:
        """Describe the managed server as a Home Assistant device."""
        host = self.host
        name = host.get("friendly_name") or host.get("hostname") or self.host_id
        os_name = " ".join(
            str(value)
            for value in (host.get("os_type"), host.get("os_version"))
            if value
        )
        return DeviceInfo(
            identifiers={
                (DOMAIN, f"{self.coordinator.entry.entry_id}_{self.host_id}")
            },
            name=str(name),
            manufacturer="PatchMon",
            model=os_name or None,
            configuration_url=self.coordinator.client.base_url,
            via_device=(DOMAIN, self.coordinator.entry.entry_id),
        )


def hub_device_info(coordinator: PatchMonCoordinator) -> DeviceInfo:
    """Describe the PatchMon server device."""
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.entry.entry_id)},
        name=coordinator.entry.title,
        manufacturer="PatchMon",
        model="Patch management server",
        configuration_url=coordinator.client.base_url,
    )
