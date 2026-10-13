"""Coordinator for PatchMon host inventory, watched packages and patch runs."""

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
    CONF_MONITOR_PATCH_RUNS,
    CONF_SCAN_INTERVAL,
    CONF_TRACKED_PACKAGES,
    DEFAULT_INCLUDE_PACKAGES,
    DEFAULT_MONITOR_PATCH_RUNS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class PatchMonCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Keep PatchMon hosts and opted-in package entities up to date."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: PatchMonClient) -> None:
        self.entry = entry
        self.client = client
        options = {**entry.data, **entry.options}
        self.host_groups = str(options.get(CONF_HOST_GROUPS, "")).strip()
        self.include_packages = bool(options.get(CONF_INCLUDE_PACKAGES, DEFAULT_INCLUDE_PACKAGES))
        self.monitor_patch_runs = bool(
            options.get(CONF_MONITOR_PATCH_RUNS, DEFAULT_MONITOR_PATCH_RUNS)
        )
        raw_tracked = options.get(CONF_TRACKED_PACKAGES, {})
        self.tracked_packages: dict[str, tuple[str, ...]] = {
            str(host_id): tuple(str(name).strip() for name in names if str(name).strip())
            for host_id, names in raw_tracked.items()
            if isinstance(names, list)
        } if isinstance(raw_tracked, dict) else {}
        # Keep the last *successful* response; network errors must not look like
        # "no updates" or erase existing entity versions.
        self._package_cache: dict[str, list[dict[str, Any]]] = {}
        self._run_cache: dict[str, dict[str, Any]] = {}
        scan_minutes = int(options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            config_entry=entry,
            update_interval=timedelta(minutes=scan_minutes),
            always_update=False,
        )

    def watched_packages(self, host_id: str) -> tuple[str, ...]:
        """Return explicit per-host watch list (case-sensitive names)."""
        return self.tracked_packages.get(host_id, ())

    def tracked_package(self, host_id: str, name: str) -> dict[str, Any] | None:
        """Look up an installed package from a successful full inventory response."""
        host = self.host(host_id)
        if not host:
            return None
        packages = host.get("_tracked_packages")
        if not isinstance(packages, list):
            return None
        return next((pkg for pkg in packages if pkg.get("name") == name), None)

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            hosts = await self.client.async_get_hosts(self.host_groups or None)
        except PatchMonAuthenticationError as err:
            raise ConfigEntryAuthFailed(f"PatchMon authentication failed: {err}") from err
        except PatchMonError as err:
            raise UpdateFailed(str(err)) from err

        host_data = {str(host["id"]): dict(host) for host in hosts}
        semaphore = asyncio.Semaphore(5)

        async def load_host(host_id: str, host: dict[str, Any]) -> None:
            tracked = bool(self.watched_packages(host_id))
            if tracked or (self.include_packages and int(host.get("updates_count") or 0) > 0):
                try:
                    async with semaphore:
                        packages = await self.client.async_get_packages(
                            host_id, updates_only=not tracked
                        )
                    self._package_cache[host_id] = packages
                    host["_packages_loaded"] = True
                except PatchMonError as err:
                    _LOGGER.warning("Could not refresh packages for %s: %s", host_id, err)
                    host["_packages_loaded"] = False
                cached = self._package_cache.get(host_id)
                if cached is not None:
                    host["_update_packages"] = [p for p in cached if p.get("needs_update", True)]
                    if tracked:
                        host["_tracked_packages"] = cached
            elif self.include_packages:
                host["_update_packages"] = []
                host["_packages_loaded"] = True

            if self.monitor_patch_runs and self.client.patching_enabled:
                try:
                    async with semaphore:
                        runs = await self.client.async_get_patch_runs(host_id, limit=1)
                    self._run_cache[host_id] = runs[0] if runs else {}
                    host["_patch_run_loaded"] = True
                except PatchMonError as err:
                    _LOGGER.warning("Could not refresh patch runs for %s: %s", host_id, err)
                    host["_patch_run_loaded"] = False
                if host_id in self._run_cache:
                    host["_patch_run"] = self._run_cache[host_id]

        await asyncio.gather(*(load_host(host_id, host) for host_id, host in host_data.items()))
        # Remove caches for deleted/inaccessible hosts.
        for cache in (self._package_cache, self._run_cache):
            for old_id in cache.keys() - host_data.keys():
                del cache[old_id]
        return host_data

    def host(self, host_id: str) -> dict[str, Any] | None:
        return (self.data or {}).get(host_id)
