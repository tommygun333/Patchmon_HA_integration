"""PatchMon integration."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig
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
    CONF_AUTO_SECURITY_HOSTS,
    DOMAIN,
    PLATFORMS,
    SERVICE_GET_HOST_UPDATES,
    SERVICE_PATCH_HOST,
)
from .coordinator import PatchMonCoordinator
from .maintenance_selection import selected_maintenance_host_ids
from .report_websocket import register_websocket
from .reporting_runtime import (
    extend_maintenance_schema,
    register_report_services,
    run_reporting_maintenance,
    run_reporting_validation,
    setup_reporting,
)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_ENTRY_ID = "config_entry_id"
SERVICE_SECURITY_MAINTENANCE = "security_maintenance"
SECURITY_REPORT_EVENT = "patchmon_security_maintenance_report"

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


SECURITY_MAINTENANCE_SCHEMA = vol.Schema(
    {
        vol.Optional(SERVICE_ENTRY_ID): cv.string,
        vol.Optional("host_ids"): vol.All(cv.ensure_list, [cv.string], vol.Length(min=1, max=30)),
        vol.Optional("mode"): vol.In(("plan", "validate", "install")),
        vol.Optional("validate"): cv.boolean,  # backwards-compatible old flag
        vol.Optional("stop_on_failure", default=True): cv.boolean,
        vol.Optional("max_report_age_minutes", default=120): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=1440)),
        vol.Optional("notify", default=True): cv.boolean,
        vol.Optional("report_source", default="automation"): vol.In(
            ("automation", "manual")
        ),
    }
)

SECURITY_MAINTENANCE_SCHEMA = extend_maintenance_schema(SECURITY_MAINTENANCE_SCHEMA)

PATCH_ALL_PREFLIGHT_SCHEMA = vol.Schema(
    {
        vol.Optional(SERVICE_ENTRY_ID): cv.string,
        vol.Required(ATTR_HOST_ID): cv.string,
        vol.Optional("max_report_age_minutes", default=120): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=1440)),
        vol.Optional("notify", default=True): cv.boolean,
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
    await setup_reporting(hass, coordinator)
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
    register_report_services(hass, _coordinator_for_call)
    register_websocket(hass)
    await hass.http.async_register_static_paths([
        StaticPathConfig(
            "/patchmon-security-reports",
            str(Path(__file__).parent / "www"),
            False,
        ),
    ])
    await panel_custom.async_register_panel(
        hass=hass,
        frontend_url_path="patchmon-reports",
        webcomponent_name="patchmon-security-report-panel",
        module_url="/patchmon-security-reports/patchmon-security-report-card.js",
        sidebar_title=None,
        require_admin=False,
    )
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
        if not call.data[ATTR_DRY_RUN] and not call.data[ATTR_PENDING_APPROVAL]:
            coordinator.follow_patch(
                call.data[ATTR_HOST_ID], call.data[ATTR_PACKAGE_NAMES] or None, result,
            )
        else:
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

    def _maintenance_lock(coordinator: PatchMonCoordinator) -> asyncio.Lock:
        """Serialize maintenance per connected PatchMon server."""
        return hass.data.setdefault(DOMAIN, {}).setdefault(
            f"maintenance_lock_{coordinator.entry.entry_id}", asyncio.Lock()
        )

    async def security_maintenance(call: ServiceCall) -> dict[str, Any]:
        """Run maintenance with persistent history, preflight and health checks."""
        coordinator = _coordinator_for_call(hass, call)
        lock = _maintenance_lock(coordinator)
        if lock.locked():
            raise ServiceValidationError("PatchMon maintenance already active on this server")
        async with lock:
            data = dict(call.data)
            if "host_ids" not in data:
                # No implicit patch-all or dynamic inclusion of newly discovered
                # hosts. A removed host blocks the *entire* automatic run.
                try:
                    data["host_ids"] = selected_maintenance_host_ids(
                        coordinator.entry.options.get(CONF_AUTO_SECURITY_HOSTS),
                        known_host_ids=set((coordinator.data or {}).keys()),
                    )
                except ValueError as err:
                    raise ServiceValidationError(str(err)) from err
            return await run_reporting_maintenance(hass, coordinator, data)

    async def validate_patch_all(call: ServiceCall) -> dict[str, Any]:
        """Manual selected-package preflight; NEVER executes Patch all."""
        coordinator = _coordinator_for_call(hass, call)
        lock = _maintenance_lock(coordinator)
        if lock.locked():
            raise ServiceValidationError("PatchMon maintenance already active on this server")
        async with lock:
            result = await run_reporting_validation(hass, coordinator, dict(call.data))
            hass.bus.async_fire("patchmon_patch_all_preflight_report", {
                "host_id": call.data[ATTR_HOST_ID],
                "status": result["status"],
                "success": result.get("success", False),
                "validation_run_id": result.get("validation", {}).get("run_id"),
                "selected_count": result.get("selected_count", 0),
            })
            return result

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

    hass.services.async_register(
        DOMAIN,
        SERVICE_SECURITY_MAINTENANCE,
        security_maintenance,
        schema=SECURITY_MAINTENANCE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "validate_patch_all",
        validate_patch_all,
        schema=PATCH_ALL_PREFLIGHT_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
