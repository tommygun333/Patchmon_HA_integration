"""Native update entities for explicitly watched PatchMon host packages."""

from __future__ import annotations

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
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
    """Create only explicitly tracked entities, including newly discovered hosts."""
    coordinator: PatchMonCoordinator = entry.runtime_data
    known: set[tuple[str, str]] = set()

    @callback
    def add_watched() -> None:
        new: list[PatchMonPackageUpdate] = []
        for host_id in (coordinator.data or {}):
            for name in coordinator.watched_packages(host_id):
                key = (host_id, name)
                if key not in known:
                    known.add(key)
                    new.append(PatchMonPackageUpdate(coordinator, host_id, name))
        if new:
            async_add_entities(new)

    add_watched()
    entry.async_on_unload(coordinator.async_add_listener(add_watched))


class PatchMonPackageUpdate(PatchMonHostEntity, UpdateEntity):
    """Represents a specific package on one specific PatchMon host."""

    _attr_icon = "mdi:package-up"

    def __init__(self, coordinator: PatchMonCoordinator, host_id: str, name: str) -> None:
        super().__init__(coordinator, host_id)
        self.package_name = name
        self._attr_name = name
        self._attr_title = name
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{host_id}_pkg_{name}"
        self._attr_supported_features = (
            UpdateEntityFeature.INSTALL if coordinator.client.patching_enabled else UpdateEntityFeature(0)
        )

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.host.get("_packages_loaded") is True
            and self.coordinator.tracked_package(self.host_id, self.package_name) is not None
        )

    @property
    def installed_version(self) -> str | None:
        package = self.coordinator.tracked_package(self.host_id, self.package_name)
        value = package.get("current_version") if package else None
        return str(value) if value else None

    @property
    def latest_version(self) -> str | None:
        package = self.coordinator.tracked_package(self.host_id, self.package_name)
        if not package:
            return None
        if not package.get("needs_update"):
            return self.installed_version
        value = package.get("available_version")
        return str(value) if value else None

    @property
    def in_progress(self) -> bool:
        """Report a matching active PatchMon run when history monitoring is on."""
        if self.host.get("_patch_run_loaded") is not True:
            return False
        run = self.host.get("_patch_run")
        if not isinstance(run, dict) or run.get("status") not in {
            "queued", "scheduled", "running", "pending_validation", "pending_approval"
        }:
            return False
        if run.get("patch_type") == "patch_all":
            return True
        package_names = run.get("package_names") or []
        return self.package_name == run.get("package_name") or (
            isinstance(package_names, list) and self.package_name in package_names
        )

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """Package manager versions may not be comparable as SemVer.

        Trust PatchMon's needs_update result instead of lexical comparison.
        """
        package = self.coordinator.tracked_package(self.host_id, self.package_name)
        return bool(package and package.get("needs_update"))

    @property
    def extra_state_attributes(self) -> dict[str, str | bool] | None:
        package = self.coordinator.tracked_package(self.host_id, self.package_name)
        if not package:
            return None
        return {
            "security_update": bool(package.get("is_security_update")),
            "needs_update": bool(package.get("needs_update")),
            "package_name": self.package_name,
            "host_id": self.host_id,
        }

    async def async_install(self, version: str | None, backup: bool, **kwargs) -> None:
        """Queue a package-specific patch; PatchMon performs the actual install."""
        if not self.coordinator.client.patching_enabled:
            raise HomeAssistantError("Configure PatchMon patch credentials before installing")
        if not self.available:
            raise HomeAssistantError("Package inventory is unavailable; refusing a blind update")
        if self.in_progress:
            raise HomeAssistantError("A PatchMon patch run for this package is already in progress")
        if version and version != self.latest_version:
            raise HomeAssistantError("PatchMon can only install the currently available version")
        if backup:
            raise HomeAssistantError("PatchMon does not offer pre-install backups through this API")
        if not self.coordinator.tracked_package(self.host_id, self.package_name).get("needs_update"):
            raise HomeAssistantError("Package is already current")
        try:
            await self.coordinator.client.async_patch_host(
                self.host_id, package_names=[self.package_name]
            )
        except PatchMonError as err:
            raise HomeAssistantError(f"Could not queue PatchMon package update: {err}") from err
        await self.coordinator.async_request_refresh()
