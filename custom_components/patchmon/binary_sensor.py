"""Binary sensor entities for PatchMon."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PatchMonCoordinator
from .entity import PatchMonHostEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PatchMon host binary sensors."""
    coordinator: PatchMonCoordinator = entry.runtime_data
    known_hosts: set[str] = set()

    @callback
    def add_hosts() -> None:
        new_hosts = set((coordinator.data or {}).keys()) - known_hosts
        if not new_hosts:
            return
        async_add_entities(
            entity
            for host_id in new_hosts
            for entity in (
                PatchMonConnectivitySensor(coordinator, host_id),
                PatchMonRebootSensor(coordinator, host_id),
            )
        )
        known_hosts.update(new_hosts)

    add_hosts()
    entry.async_on_unload(coordinator.async_add_listener(add_hosts))


class PatchMonConnectivitySensor(PatchMonHostEntity, BinarySensorEntity):
    """Whether a managed host is active in PatchMon."""

    _attr_name = "Connectivity"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: PatchMonCoordinator, host_id: str) -> None:
        super().__init__(coordinator, host_id)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{host_id}_connectivity"

    @property
    def is_on(self) -> bool | None:
        """Return the effective PatchMon status."""
        status = self.host.get("effective_status")
        return status == "active" if status else None


class PatchMonRebootSensor(PatchMonHostEntity, BinarySensorEntity):
    """Whether a managed host needs a reboot."""

    _attr_name = "Reboot required"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:restart-alert"

    def __init__(self, coordinator: PatchMonCoordinator, host_id: str) -> None:
        super().__init__(coordinator, host_id)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{host_id}_reboot_required"

    @property
    def is_on(self) -> bool:
        """Return whether PatchMon reports a pending reboot."""
        return bool(self.host.get("needs_reboot"))
