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
from .patch_utils import ACTIVE_RUN_STATUSES, SUCCESS_RUN_STATUSES
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
        self._follow_tasks: dict[str, asyncio.Task[None]] = {}
        # Register once, not once per submitted run (long-lived HA processes
        # could otherwise accumulate unload callbacks indefinitely).
        entry.async_on_unload(self._cancel_follow_tasks)
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
                    latest_run = runs[0] if runs else {}
                    previous_run = self._run_cache.get(host_id, {})
                    if (
                        latest_run.get("id") == previous_run.get("id")
                        and latest_run.get("status") == previous_run.get("status")
                        and previous_run.get("_ha_phase")
                    ):
                        latest_run = {**latest_run, "_ha_phase": previous_run["_ha_phase"]}
                    # The normal fleet poll must not displace a run we are
                    # actively following with an unrelated old history entry.
                    task = self._follow_tasks.get(host_id)
                    if not (task and not task.done() and previous_run.get("id") != latest_run.get("id")):
                        self._run_cache[host_id] = latest_run
                    host["_patch_run_loaded"] = True
                except PatchMonError as err:
                    _LOGGER.warning("Could not refresh patch runs for %s: %s", host_id, err)
                    host["_patch_run_loaded"] = False
                if host_id in self._run_cache:
                    host["_patch_run"] = self._run_cache[host_id]
            # A post-patch report can arrive after the bounded follow task
            # stopped. A normal coordinator poll should clear that warning
            # once the watched package is confirmed current.
            run = self._run_cache.get(host_id)
            if (
                run and run.get("_ha_phase") == "awaiting_inventory"
                and host.get("_packages_loaded") is True
            ):
                names = run.get("package_names") or (
                    [run["package_name"]] if run.get("package_name") else []
                )
                packages = host.get("_tracked_packages", [])
                if names and all(
                    any(p.get("name") == name and not p.get("needs_update") for p in packages)
                    for name in names
                ):
                    run = {**run, "_ha_phase": "completed"}
                    self._run_cache[host_id] = run
            # Preserve HA-initiated runs across normal coordinator polls even
            # when fleet-wide patch-run monitoring has been disabled.
            if run is not None:
                host["_patch_run"] = run
                host["_patch_run_loaded"] = True

        await asyncio.gather(*(load_host(host_id, host) for host_id, host in host_data.items()))
        # Remove caches for deleted/inaccessible hosts.
        for cache in (self._package_cache, self._run_cache):
            for old_id in cache.keys() - host_data.keys():
                del cache[old_id]
        return host_data

    def host(self, host_id: str) -> dict[str, Any] | None:
        return (self.data or {}).get(host_id)

    def publish_patch_run(self, host_id: str, run: dict[str, Any]) -> None:
        """Update the live HA state without awaiting the regular 15-minute poll."""
        self._run_cache[host_id] = run
        if self.data and host_id in self.data:
            data = dict(self.data)
            host = dict(data[host_id])
            host["_patch_run"] = run
            host["_patch_run_loaded"] = True
            data[host_id] = host
            self.async_set_updated_data(data)

    def follow_patch(self, host_id: str, package_names: list[str] | None, result: dict[str, Any]) -> None:
        """Track a successfully queued run in the background, using its real ID."""
        run_id = result.get("patch_run_id")
        if not isinstance(run_id, str) or not run_id:
            _LOGGER.warning("PatchMon did not return patch_run_id; cannot follow run")
            return
        names = package_names or []
        package_name = names[0] if len(names) == 1 else None
        run = {
            "id": run_id,
            "host_id": host_id,
            "status": "pending_approval" if result.get("pending_approval") else "queued",
            "patch_type": "patch_package" if names else "patch_all",
            "package_names": names,
            "scheduled_at": result.get("run_at"),
        }
        self.publish_patch_run(host_id, run)
        previous = self._follow_tasks.get(host_id)
        if previous and not previous.done():
            previous.cancel()
        task = self.hass.async_create_background_task(
            self._watch_patch_run(host_id, package_name, run_id),
            f"PatchMon follow patch run {run_id}",
        )
        self._follow_tasks[host_id] = task
        task.add_done_callback(lambda completed: self._clear_task(host_id, completed))

    def _cancel_follow_tasks(self) -> None:
        for task in self._follow_tasks.values():
            task.cancel()
        self._follow_tasks.clear()

    def _clear_task(self, host_id: str, task: asyncio.Task[None]) -> None:
        if self._follow_tasks.get(host_id) is task:
            self._follow_tasks.pop(host_id, None)

    async def _refresh_one_host_packages(self, host_id: str, package_name: str | None) -> bool:
        """Refresh only the affected host; avoid polling the entire fleet."""
        tracked = bool(self.watched_packages(host_id))
        packages = await self.client.async_get_packages(host_id, updates_only=not tracked)
        self._package_cache[host_id] = packages
        if self.data and host_id in self.data:
            data = dict(self.data)
            host = dict(data[host_id])
            host["_packages_loaded"] = True
            host["_update_packages"] = [p for p in packages if p.get("needs_update", True)]
            if tracked:
                host["_tracked_packages"] = packages
            data[host_id] = host
            self.async_set_updated_data(data)
        if package_name is None:
            return True
        return any(
            pkg.get("name") == package_name and not pkg.get("needs_update")
            for pkg in packages
        )

    async def _watch_patch_run(self, host_id: str, package_name: str | None, run_id: str) -> None:
        """Publish PatchMon run stages, then confirm the agent package inventory.

        A completed run is not yet a confirmed version update. The stages are
        exposed to HA as coarse milestones, not actual installation percent.
        """
        terminal: dict[str, Any] | None = None
        for _ in range(120):  # Up to ~20 min (including scheduled starts).
            await asyncio.sleep(10)
            try:
                run = await self.client.async_get_patch_run(run_id)
            except PatchMonError as err:
                _LOGGER.debug("Cannot refresh PatchMon run %s: %s", run_id, err)
                continue
            if run.get("status") not in ACTIVE_RUN_STATUSES:
                terminal = run
                if run.get("status") in SUCCESS_RUN_STATUSES:
                    # Keep the progress indicator active while verifying the
                    # post-install inventory rather than reporting completion
                    # prematurely.
                    self.publish_patch_run(host_id, {**run, "_ha_phase": "verifying"})
                else:
                    self.publish_patch_run(host_id, {**run, "_ha_phase": "errored"})
                break
            self.publish_patch_run(host_id, run)
        if terminal is None:
            # Timed-out/long-scheduled runs are still tracked by regular polls.
            return
        success = terminal.get("status") in SUCCESS_RUN_STATUSES
        # Refresh once even on failure: a partially successful run can still
        # have updated packages. On success, wait for PatchMon's new report.
        attempts = 36 if success and package_name else 1
        confirmed = False
        for attempt in range(attempts):
            try:
                confirmed = await self._refresh_one_host_packages(host_id, package_name)
                if confirmed:
                    break
            except PatchMonError as err:
                _LOGGER.debug("Waiting for PatchMon post-patch report from %s: %s", host_id, err)
            if attempt + 1 < attempts:
                await asyncio.sleep(10)
        if success:
            phase = "completed" if confirmed else "awaiting_inventory"
            if not confirmed:
                _LOGGER.warning(
                    "PatchMon run %s completed, but %s did not confirm its new inventory",
                    run_id, host_id,
                )
            self.publish_patch_run(host_id, {**terminal, "_ha_phase": phase})
