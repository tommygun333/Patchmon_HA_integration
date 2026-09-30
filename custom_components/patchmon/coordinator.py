"""Data update coordinator for PatchMon."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PatchMonAuthenticationError, PatchMonClient, PatchMonError
from .const import (
    CONF_HOST_GROUPS,
    CONF_INCLUDE_PACKAGES,
    CONF_SCAN_INTERVAL,
    DEFAULT_INCLUDE_PACKAGES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class PatchMonCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Keep the PatchMon host inventory current."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: PatchMonClient
    ) -> None:
        self.entry = entry
        self.client = client
        options = {**entry.data, **entry.options}
        self.host_groups = str(options.get(CONF_HOST_GROUPS, "")).strip()
        self.include_packages = bool(
            options.get(CONF_INCLUDE_PACKAGES, DEFAULT_INCLUDE_PACKAGES)
        )
        scan_minutes = int(options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            config_entry=entry,
            update_interval=timedelta(minutes=scan_minutes),
            always_update=False,
        )

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            hosts = await self.client.async_get_hosts(self.host_groups or None)
        except PatchMonAuthenticationError as err:
            raise ConfigEntryAuthFailed(
                f"PatchMon authentication failed: {err}"
            ) from err
        except PatchMonError as err:
            raise UpdateFailed(str(err)) from err

        host_data = {str(host["id"]): dict(host) for host in hosts}
        if self.include_packages:
            semaphore = asyncio.Semaphore(5)

            async def load_packages(host_id: str, host: dict[str, Any]) -> None:
                if int(host.get("updates_count") or 0) <= 0:
                    host["_update_packages"] = []
                    return
                try:
                    async with semaphore:
                        host["_update_packages"] = await self.client.async_get_updates(
                            host_id
                        )
                except PatchMonError as err:
                    _LOGGER.warning(
                        "Could not load update packages for PatchMon host %s: %s",
                        host_id,
                        err,
                    )

            await asyncio.gather(
                *(load_packages(host_id, host) for host_id, host in host_data.items())
            )
        return host_data

    def host(self, host_id: str) -> dict[str, Any] | None:
        """Return current data for one host."""
        return (self.data or {}).get(host_id)
