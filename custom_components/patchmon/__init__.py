"""PatchMon integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import PatchMonClient, PatchMonError
from .const import (
    ATTR_DRY_RUN,
    ATTR_HOST_ID,
    ATTR_PACKAGE_NAMES,
    ATTR_PENDING_APPROVAL,
    ATTR_RUN_IMMEDIATELY,
    CONF_API_ID,
    CONF_API_KEY,
    DOMAIN,
    PLATFORMS,
    SERVICE_GET_HOST_UPDATES,
    SERVICE_PATCH_HOST,
)
from .coordinator import PatchMonCoordinator

SERVICE_ENTRY_ID = "config_entry_id"

PATCH_HOST_SCHEMA = vol.Schema(
    {
        vol.Optional(SERVICE_ENTRY_ID): cv.string,
        vol.Required(ATTR_HOST_ID): cv.string,
        vol.Optional(ATTR_PACKAGE_NAMES, default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
        vol.Optional(ATTR_PENDING_APPROVAL, default=False): cv.boolean,
        vol.Optional(ATTR_RUN_IMMEDIATELY, default=False): cv.boolean,
    }
)

GET_UPDATES_SCHEMA = vol.Schema(
    {
        vol.Optional(SERVICE_ENTRY_ID): cv.string,
        vol.Required(ATTR_HOST_ID): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PatchMon from a config entry."""
    client = PatchMonClient(
        async_get_clientsession(hass),
        entry.data[CONF_URL],
        entry.data[CONF_API_ID],
        entry.data[CONF_API_KEY],
        verify_ssl=entry.data.get(CONF_VERIFY_SSL, True),
        username=entry.data.get(CONF_USERNAME),
        password=entry.data.get(CONF_PASSWORD),
    )
    coordinator = PatchMonCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a PatchMon config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _coordinator_for_call(hass: HomeAssistant, call: ServiceCall) -> PatchMonCoordinator:
    """Resolve the loaded PatchMon entry selected by a service call."""
    entry_id = call.data.get(SERVICE_ENTRY_ID)
    if entry_id:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(f"Unknown PatchMon config entry: {entry_id}")
        if entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError("The selected PatchMon entry is not loaded")
        return entry.runtime_data

    loaded_entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if len(loaded_entries) == 1:
        return loaded_entries[0].runtime_data
    if not loaded_entries:
        raise ServiceValidationError("No loaded PatchMon config entry is available")
    raise ServiceValidationError(
        "config_entry_id is required when more than one PatchMon server is configured"
    )


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """Set up PatchMon actions."""
    _register_services(hass)
    return True


def _register_services(hass: HomeAssistant) -> None:
    """Register integration actions shared by all entries."""

    async def patch_host(call: ServiceCall) -> dict[str, Any]:
        coordinator = _coordinator_for_call(hass, call)
        try:
            result = await coordinator.client.async_patch_host(
                call.data[ATTR_HOST_ID],
                package_names=call.data[ATTR_PACKAGE_NAMES],
                dry_run=call.data[ATTR_DRY_RUN],
                pending_approval=call.data[ATTR_PENDING_APPROVAL],
                run_immediately=call.data[ATTR_RUN_IMMEDIATELY],
            )
        except PatchMonError as err:
            raise HomeAssistantError(f"Could not start PatchMon patch run: {err}") from err
        await coordinator.async_request_refresh()
        return result

    async def get_host_updates(call: ServiceCall) -> dict[str, Any]:
        coordinator = _coordinator_for_call(hass, call)
        host_id = call.data[ATTR_HOST_ID]
        try:
            packages = await coordinator.client.async_get_updates(host_id)
        except PatchMonError as err:
            raise HomeAssistantError(f"Could not query PatchMon packages: {err}") from err
        return {
            "host_id": host_id,
            "total": len(packages),
            "packages": packages,
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_PATCH_HOST,
        patch_host,
        schema=PATCH_HOST_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_HOST_UPDATES,
        get_host_updates,
        schema=GET_UPDATES_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
