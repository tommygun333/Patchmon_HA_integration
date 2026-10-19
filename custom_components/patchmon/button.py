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
from .patch_utils import ACTIVE_RUN_STATUSES, RUN_STAGE_PROGRESS


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

    @property
    def extra_state_attributes(self) -> dict | None:
        """Expose the host-wide run for dashboard console/status popups."""
        run = self.host.get("_patch_run") or {}
        if run.get("patch_type") != "patch_all":
            return None
        phase = run.get("_ha_phase")
        status = str(run.get("status") or "")
        stage = phase or RUN_STAGE_PROGRESS.get(status, (status, None))[0]
        return {
            "patch_run_id": run.get("id"),
            "patch_stage": stage,
            "stage_milestone": 90 if phase == "verifying" else (
                RUN_STAGE_PROGRESS.get(status, (None, None))[1]
                if not phase else None
            ),
            "in_progress": phase == "verifying" or (
                not phase and status in ACTIVE_RUN_STATUSES
            ),
            "console_streaming": bool(run.get("_ha_console_live")),
            "console_sensor_unique_id": (
                f"{self.coordinator.entry.entry_id}_{self.host_id}_patch_console"
            ),
        }

    async def async_press(self) -> None:
        """Queue patch-all once and follow its real run through verification."""
        run = self.host.get("_patch_run") or {}
        if run.get("status") in ACTIVE_RUN_STATUSES or run.get("_ha_phase") == "verifying":
            raise HomeAssistantError("A PatchMon patch run is already active for this host")
        try:
            result = await self.coordinator.client.async_patch_host(self.host_id)
        except PatchMonError as err:
            raise HomeAssistantError(f"Could not start PatchMon patch run: {err}") from err
        # An immediate inventory refresh would still contain the old versions.
        # The watcher waits for the agent's post-patch inventory report.
        self.coordinator.follow_patch(self.host_id, None, result)
