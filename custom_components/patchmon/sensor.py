"""Fleet, host and patch-run sensor entities for PatchMon."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import PACKAGE_ATTRIBUTE_LIMIT
from .coordinator import PatchMonCoordinator
from .entity import PatchMonHostEntity, hub_device_info
from .patch_utils import console_tail


FLEET_SENSORS: dict[str, tuple[str, str, Callable[[dict[str, dict[str, Any]]], int]]] = {
    "total_hosts": ("Total hosts", "mdi:server", lambda hosts: len(hosts)),
    "hosts_needing_updates": (
        "Hosts needing updates", "mdi:server-security",
        lambda hosts: sum(int(h.get("updates_count") or 0) > 0 for h in hosts.values()),
    ),
    "total_updates": (
        "Available updates", "mdi:package-up",
        lambda hosts: sum(int(h.get("updates_count") or 0) for h in hosts.values()),
    ),
    "security_updates": (
        "Security updates", "mdi:shield-alert",
        lambda hosts: sum(int(h.get("security_updates_count") or 0) for h in hosts.values()),
    ),
    "offline_hosts": (
        "Offline hosts", "mdi:server-off",
        lambda hosts: sum(h.get("effective_status") == "inactive" for h in hosts.values()),
    ),
    "hosts_needing_reboot": (
        "Hosts needing reboot", "mdi:restart-alert",
        lambda hosts: sum(bool(h.get("needs_reboot")) for h in hosts.values()),
    ),
}

HOST_NUMBER_SENSORS: dict[str, tuple[str, str, str]] = {
    "updates_count": ("Available updates", "mdi:package-up", "updates_count"),
    "security_updates_count": ("Security updates", "mdi:shield-alert", "security_updates_count"),
    "total_packages": ("Installed packages", "mdi:package-variant", "total_packages"),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PatchMonCoordinator = entry.runtime_data
    async_add_entities(
        PatchMonFleetSensor(coordinator, key, name, icon, value_fn)
        for key, (name, icon, value_fn) in FLEET_SENSORS.items()
    )
    known_hosts: set[str] = set()

    @callback
    def add_hosts() -> None:
        new_hosts = set((coordinator.data or {}).keys()) - known_hosts
        if not new_hosts:
            return
        entities: list[SensorEntity] = []
        for host_id in new_hosts:
            entities.extend(
                PatchMonHostNumberSensor(coordinator, host_id, key, name, icon, field)
                for key, (name, icon, field) in HOST_NUMBER_SENSORS.items()
            )
            entities.append(PatchMonLastReportSensor(coordinator, host_id))
            if coordinator.client.patching_enabled:
                entities.extend((
                    PatchMonLastPatchRunSensor(coordinator, host_id),
                    PatchMonPatchConsoleSensor(coordinator, host_id),
                ))
        known_hosts.update(new_hosts)
        async_add_entities(entities)

    add_hosts()
    entry.async_on_unload(coordinator.async_add_listener(add_hosts))


class PatchMonFleetSensor(CoordinatorEntity[PatchMonCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PatchMonCoordinator,
        key: str,
        name: str,
        icon: str,
        value_fn: Callable[[dict[str, dict[str, Any]]], int],
    ) -> None:
        super().__init__(coordinator)
        self._value_fn = value_fn
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon

    @property
    def native_value(self) -> int:
        return self._value_fn(self.coordinator.data or {})

    @property
    def device_info(self):
        return hub_device_info(self.coordinator)


class PatchMonHostNumberSensor(PatchMonHostEntity, SensorEntity):
    def __init__(
        self, coordinator: PatchMonCoordinator, host_id: str,
        key: str, name: str, icon: str, field: str
    ) -> None:
        super().__init__(coordinator, host_id)
        self._field = field
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{host_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon

    @property
    def native_value(self) -> int | None:
        value = self.host.get(self._field)
        return int(value) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._field != "updates_count":
            return None
        packages = self.host.get("_update_packages")
        attrs: dict[str, Any] = {
            "host_id": self.host_id,
            "hostname": self.host.get("hostname"),
            "ip_address": self.host.get("ip"),
            "host_groups": [
                group.get("name")
                for group in self.host.get("host_groups", [])
                if isinstance(group, dict) and group.get("name")
            ],
            "status": self.host.get("effective_status"),
            "reporting_state": self.host.get("reporting_state"),
            "update_state": self.host.get("update_state"),
            "package_data_fresh": self.host.get("_packages_loaded"),
        }
        if isinstance(packages, list):
            visible = packages[:PACKAGE_ATTRIBUTE_LIMIT]
            attrs["packages"] = [
                {
                    "name": package.get("name"),
                    "current_version": package.get("current_version"),
                    "available_version": package.get("available_version"),
                    "security": bool(package.get("is_security_update")),
                }
                for package in visible
            ]
            attrs["packages_truncated"] = len(packages) > PACKAGE_ATTRIBUTE_LIMIT
        return attrs


class PatchMonLastReportSensor(PatchMonHostEntity, SensorEntity):
    _attr_name = "Last report"
    _attr_icon = "mdi:clock-check-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: PatchMonCoordinator, host_id: str) -> None:
        super().__init__(coordinator, host_id)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{host_id}_last_report"

    @property
    def native_value(self) -> datetime | None:
        value = self.host.get("last_update")
        return dt_util.parse_datetime(str(value)) if value else None


class PatchMonLastPatchRunSensor(PatchMonHostEntity, SensorEntity):
    """Status of the latest patch run, not generic background agent work."""

    _attr_name = "Last patch run"
    _attr_icon = "mdi:progress-wrench"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: PatchMonCoordinator, host_id: str) -> None:
        super().__init__(coordinator, host_id)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{host_id}_last_patch_run"

    @property
    def available(self) -> bool:
        return super().available and self.host.get("_patch_run_loaded") is True

    @property
    def native_value(self) -> str | None:
        run = self.host.get("_patch_run")
        return str(run.get("status")) if run and run.get("status") else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        run = self.host.get("_patch_run")
        if not isinstance(run, dict) or not run:
            return None
        # Avoid putting shell output into recorder attributes.
        attrs = {
            key: run.get(key)
            for key in (
                "id", "patch_type", "package_name", "package_names",
                "error_message", "created_at", "started_at", "completed_at", "scheduled_at",
            )
            if run.get(key) is not None
        }
        if run.get("_ha_phase"):
            attrs["update_stage"] = run["_ha_phase"]
        # A bounded tail is safe for recorder; full output is available on
        # the PatchMon patch-run detail page.
        raw_output = run.get("shell_output")
        if run.get("_ha_console_live") or not isinstance(raw_output, str):
            raw_output = run.get("_ha_console_output")
        tail = console_tail(raw_output)
        if tail:
            attrs["console_tail"] = tail
        return attrs


class PatchMonPatchConsoleSensor(PatchMonHostEntity, SensorEntity):
    """One HA state update per completed console line, plus full run output."""

    _attr_name = "Patch console"
    _attr_icon = "mdi:console-line"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_force_update = True  # Identical successive log lines still emit changes.

    def __init__(self, coordinator: PatchMonCoordinator, host_id: str) -> None:
        super().__init__(coordinator, host_id)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{host_id}_patch_console"
        self._line: str | None = None
        self._line_run_id: str | None = None
        self._line_sequence: int = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.add_console_line_listener(self.host_id, self._on_line)
        )

    def _on_line(self, run_id: str, line: str, sequence: int) -> None:
        self._line_run_id = run_id
        self._line = line
        self._line_sequence = sequence
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return super().available and self.host.get("_patch_run_loaded") is True

    @property
    def native_value(self) -> str | None:
        run = self.coordinator.console_run(self.host_id)
        if not run:
            return None
        if self._line_run_id == run.get("id") and self._line is not None:
            value = self._line
        else:
            value = run.get("_ha_console_last_line")
            if value is None:
                output = run.get("shell_output") or ""
                value = output.splitlines()[-1] if output.splitlines() else "waiting for output"
        # Home Assistant sensor states cannot exceed 255 characters.
        return str(value)[-255:]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        run = self.coordinator.console_run(self.host_id)
        if not run:
            return None
        output = run.get("_ha_console_output")
        persisted = run.get("shell_output")
        if not isinstance(output, str):
            output = persisted if isinstance(persisted, str) else ""
        elif isinstance(persisted, str) and persisted.startswith(output):
            # The terminal REST response may include a final chunk that was
            # not received on the socket before it closed.
            output = persisted
        line = self._line if self._line_run_id == run.get("id") else run.get("_ha_console_last_line")
        return {
            "run_id": run.get("id"),
            "line_number": self._line_sequence if self._line_run_id == run.get("id") else run.get("_ha_console_line_number", 0),
            "line_full": line,
            "console_output": output,  # Complete run, NOT a rolling preview.
            "console_streaming": bool(run.get("_ha_console_live")),
            "update_stage": run.get("_ha_phase") or run.get("status"),
            "error_message": run.get("error_message"),
        }
