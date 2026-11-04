"""PatchMon integration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.components import persistent_notification

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
from .security_maintenance import prepare_security_plan, validate_security_plan

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
        vol.Required("host_ids"): vol.All(cv.ensure_list, [cv.string], vol.Length(min=1, max=30)),
        vol.Optional("validate", default=True): cv.boolean,
        vol.Optional("max_report_age_minutes", default=120): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=1440)
        ),
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

    async def security_maintenance(call: ServiceCall) -> dict[str, Any]:
        """Plan security updates and optionally validate them; never install."""
        coordinator = _coordinator_for_call(hass, call)
        lock = hass.data.setdefault(DOMAIN, {}).setdefault(
            "security_maintenance_lock", asyncio.Lock()
        )
        if lock.locked():
            raise ServiceValidationError("A PatchMon security maintenance run is already active")

        async with lock:
            started = datetime.now(timezone.utc).isoformat()
            host_results: list[dict[str, Any]] = []
            for host_id in dict.fromkeys(call.data["host_ids"]):
                host = coordinator.host(host_id)
                try:
                    if not host:
                        raise ValueError("Host is not present in the current PatchMon inventory")
                    packages = await coordinator.client.async_get_updates(host_id)
                    plan = prepare_security_plan(
                        host_id, host, packages,
                        max_report_age_minutes=call.data["max_report_age_minutes"],
                    )
                    if plan["state"] == "ready" and call.data["validate"]:
                        try:
                            plan["validation"] = await validate_security_plan(
                                coordinator.client, plan,
                            )
                        except (PatchMonError, asyncio.TimeoutError) as err:
                            plan["validation"] = {
                                "status": "failed", "success": False,
                                "reason": str(err)[:400],
                            }
                    else:
                        plan["validation"] = {
                            "status": "skipped", "success": False,
                            "reason": "Validation disabled" if not call.data["validate"] else plan["state"],
                        }
                except (PatchMonError, ValueError) as err:
                    plan = {
                        "host_id": host_id, "hostname": str((host or {}).get("hostname") or host_id),
                        "state": "blocked", "security_count": 0, "regular_count": 0,
                        "execution_safe": False, "errors": [str(err)[:400]],
                        "validation": {"status": "skipped", "success": False},
                    }
                host_results.append(plan)

            summary = {
                "hosts": len(host_results),
                "ready": sum(p["state"] == "ready" for p in host_results),
                "no_security_updates": sum(
                    p["state"] == "no_security_updates" for p in host_results
                ),
                "blocked": sum(p["state"] == "blocked" for p in host_results),
                "validated": sum(
                    p["validation"].get("success") is True for p in host_results
                ),
                "failed_validation": sum(
                    p["validation"].get("status") not in ("skipped", "validated", "dry_run_completed")
                    for p in host_results
                ),
            }
            report = {
                "started_at": started,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "config_entry_id": coordinator.entry.entry_id,
                "mode": "validation" if call.data["validate"] else "plan_only",
                "installed_packages": 0,
                "execution_safe": False,
                "summary": summary,
                "hosts": host_results,
            }
            # The action response is the complete structured report. Emit a
            # Recorder-visible event and a brief persistent notification too.
            hass.bus.async_fire(SECURITY_REPORT_EVENT, {
                "started_at": started,
                "completed_at": report["completed_at"],
                "mode": report["mode"],
                "summary": summary,
                "hosts": [
                    {
                        "host_id": item["host_id"],
                        "hostname": item["hostname"],
                        "state": item["state"],
                        "security_count": item["security_count"],
                        "validation_status": item["validation"]["status"],
                        "validation_run_id": item["validation"].get("run_id"),
                    }
                    for item in host_results
                ],
            })
            if call.data["notify"]:
                lines = [
                    f"**PatchMon security maintenance (validation only)**",
                    f"Hosts: {summary['hosts']}, validated: {summary['validated']}, "
                    f"no security updates: {summary['no_security_updates']}, "
                    f"blocked: {summary['blocked']}, validation errors: {summary['failed_validation']}.",
                    "No packages were installed. Security-only execution is not yet configured.",
                ]
                for plan in host_results:
                    lines.append(
                        f"- {plan['hostname']}: {plan['state']} "
                        f"({plan['security_count']} security; "
                        f"validation: {plan['validation']['status']})"
                    )
                persistent_notification.async_create(
                    hass, "\n".join(lines),
                    title="PatchMon security maintenance report",
                    notification_id="patchmon_security_maintenance",
                )
            return report

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
