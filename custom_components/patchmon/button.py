"""Button entities for PatchMon."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import PatchMonError
from .coordinator import PatchMonCoordinator
from .entity import PatchMonHostEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up patch buttons when user credentials are configured."""
    coordinator: PatchMonCoordinator = entry.runtime_data
    if not coordinator.client.patching_enabled:
        return

    known_hosts: set[str] = set()

    @callback
    def add_hosts() -> None:
        known_hosts.difference_update(coordinator.removed_host_ids)
        new_hosts = set((coordinator.data or {}).keys()) - known_hosts
        if not new_hosts:
            return
        async_add_entities(
            PatchMonPatchAllButton(coordinator, host_id) for host_id in new_hosts
        )
        known_hosts.update(new_hosts)

    add_hosts()
    entry.async_on_unload(coordinator.async_add_listener(add_hosts))


class PatchMonPatchAllButton(PatchMonHostEntity, ButtonEntity):
    """Start a policy-controlled patch-all run for a host."""

    _attr_name = "Patch all packages"
    _attr_icon = "mdi:package-up"

    def __init__(self, coordinator: PatchMonCoordinator, host_id: str) -> None:
        super().__init__(coordinator, host_id)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{host_id}_patch_all"

    async def async_press(self) -> None:
        """Start the patch run and refresh the inventory."""
        try:
            await self.coordinator.client.async_patch_host(self.host_id)
        except PatchMonError as err:
            raise HomeAssistantError(f"Could not start PatchMon patch run: {err}") from err
        await self.coordinator.async_request_refresh()
