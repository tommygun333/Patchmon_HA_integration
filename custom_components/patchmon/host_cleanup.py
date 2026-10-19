"""Clean up registry entries for hosts absent from the PatchMon inventory.

Only PatchMon-owned entries belonging to this particular config entry are removed.
The coordinator confirms absence in two successful inventories before calling us.
"""

from __future__ import annotations

import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN

# PatchMon host IDs are UUIDs. This also finds legacy entities whose devices
# were manually removed before this cleanup was installed.
_HOST_UUID = re.compile(
    r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})_"
)


def _entity_host_id(unique_id: str, entry_id: str) -> str | None:
    prefix = f"{entry_id}_"
    if not unique_id.startswith(prefix):
        return None
    match = _HOST_UUID.match(unique_id[len(prefix):])
    return match.group(1).lower() if match else None


def _device_host_id(device: dr.DeviceEntry, entry_id: str) -> str | None:
    prefix = f"{entry_id}_"
    for domain, identifier in device.identifiers:
        if domain == DOMAIN and identifier.startswith(prefix):
            return identifier[len(prefix):]
    return None


def registered_host_ids(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    """Find hosts owned by this entry, including orphaned registry entities."""
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    ids = {
        host_id
        for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
        if (host_id := _device_host_id(device, entry.entry_id)) is not None
    }
    ids.update(
        host_id
        for entity in er.async_entries_for_config_entry(entities, entry.entry_id)
        if entity.platform == DOMAIN
        if (host_id := _entity_host_id(entity.unique_id, entry.entry_id)) is not None
    )
    return ids


def prune_missing_hosts(hass: HomeAssistant, entry: ConfigEntry, missing: set[str]) -> set[str]:
    """Remove only PatchMon entries for confirmed absent hosts.

    Leave a device in place if any unrelated (even disabled) entity or child
    device still references it. Never delete the PatchMon hub device.
    """
    if not missing:
        return set()
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    host_devices = {
        device.id: host_id
        for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
        if (host_id := _device_host_id(device, entry.entry_id)) in missing
    }
    removed_entities = 0
    for entity in list(er.async_entries_for_config_entry(entities, entry.entry_id)):
        if entity.platform != DOMAIN:
            continue
        host_id = _entity_host_id(entity.unique_id, entry.entry_id)
        # Device-linked fallback supports non-UUID host IDs. The unique-ID
        # prefix still protects hub and unrelated entities manually assigned
        # to the missing host's device.
        if host_id not in missing:
            host_id = host_devices.get(entity.device_id)
            if host_id is None or not entity.unique_id.startswith(
                f"{entry.entry_id}_{host_id}_"
            ):
                continue
        entities.async_remove(entity.entity_id)
        removed_entities += 1

    removed_devices = 0
    for device_id in host_devices:
        if er.async_entries_for_device(entities, device_id, include_disabled_entities=True):
            continue
        if dr.async_entries_for_parent_device(devices, device_id):
            continue
        devices.async_remove_device(device_id)
        removed_devices += 1

    # Returning the confirmed IDs lets discovery platforms forget their cached
    # IDs and recreate entities should the same host return later.
    return set(missing)
