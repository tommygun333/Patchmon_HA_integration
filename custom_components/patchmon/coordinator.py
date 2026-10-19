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
from .host_cleanup import prune_missing_hosts, registered_host_ids
from .patch_utils import (
    ACTIVE_RUN_STATUSES, SUCCESS_RUN_STATUSES, post_patch_report_is_fresh,
)
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
        self._stream_tasks: dict[str, asyncio.Task[None]] = {}
        self._stream_run_ids: dict[str, str] = {}
        self._console_flush_tasks: dict[str, asyncio.Task[None]] = {}
        self._console_line_listeners: dict[str, set] = {}
        self._missing_host_confirmations: dict[str, int] = {}
        self.removed_host_ids: frozenset[str] = frozenset()
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
                    if latest_run.get("id") == previous_run.get("id"):
                        latest_run = dict(latest_run)
                        for key in (
                            "_ha_phase", "_ha_target_packages", "_ha_before_last_update",
                        ):
                            if key in previous_run and key not in latest_run:
                                latest_run[key] = previous_run[key]
                    # The normal fleet poll must not displace a run we are
                    # actively following with an unrelated old history entry.
                    task = self._follow_tasks.get(host_id)
                    if not (task and not task.done() and previous_run.get("id") != latest_run.get("id")):
                        if latest_run.get("id") == previous_run.get("id"):
                            latest_run = dict(latest_run)
                            for key in ("_ha_console_output", "_ha_console_live", "_ha_console_pending",
                                        "_ha_console_last_line", "_ha_console_line_number"):
                                if key in previous_run and key not in latest_run:
                                    latest_run[key] = previous_run[key]
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
                if run.get("patch_type") == "patch_all":
                    verified = post_patch_report_is_fresh(
                        run.get("_ha_before_last_update"),
                        host.get("last_update"), run.get("completed_at"),
                    )
                else:
                    names = run.get("package_names") or (
                        [run["package_name"]] if run.get("package_name") else []
                    )
                    packages = host.get("_tracked_packages", [])
                    verified = bool(names) and all(
                        any(p.get("name") == name and not p.get("needs_update") for p in packages)
                        for name in names
                    )
                if verified:
                    run = {**run, "_ha_phase": "completed"}
                    self._run_cache[host_id] = run
            # Preserve HA-initiated runs across normal coordinator polls even
            # when fleet-wide patch-run monitoring has been disabled.
            if run is not None:
                host["_patch_run"] = run
                host["_patch_run_loaded"] = True

            # Also stream PatchMon-initiated runs if run monitoring is enabled.
            # HA-initiated updates start their stream in _watch_patch_run.
            if (
                self.monitor_patch_runs
                and run and run.get("status") == "running" and run.get("id")
            ):
                self._ensure_console_stream(host_id, str(run["id"]))

        await asyncio.gather(*(load_host(host_id, host) for host_id, host in host_data.items()))
        # Remove caches for deleted/inaccessible hosts.
        for cache in (self._package_cache, self._run_cache):
            for old_id in cache.keys() - host_data.keys():
                del cache[old_id]

        # Only a successfully retrieved host inventory can confirm removal.
        # Two consecutive omissions avoid deleting entities for a transient
        # incomplete response. Failed API calls never reach this section.
        missing = registered_host_ids(self.hass, self.entry) - set(host_data)
        self._missing_host_confirmations = {
            host_id: self._missing_host_confirmations.get(host_id, 0) + 1
            for host_id in missing
        }
        confirmed = {
            host_id for host_id, count in self._missing_host_confirmations.items()
            if count >= 2
        }
        self.removed_host_ids = frozenset()
        if confirmed:
            self.removed_host_ids = frozenset(
                prune_missing_hosts(self.hass, self.entry, confirmed)
            )
            for host_id in self.removed_host_ids:
                for tasks in (
                    self._follow_tasks, self._stream_tasks, self._console_flush_tasks
                ):
                    task = tasks.pop(host_id, None)
                    if task is not None:
                        task.cancel()
                self._stream_run_ids.pop(host_id, None)
            _LOGGER.info(
                "Removed stale PatchMon host entities for %d host(s)",
                len(self.removed_host_ids),
            )
        return host_data

    def host(self, host_id: str) -> dict[str, Any] | None:
        return (self.data or {}).get(host_id)

    def publish_patch_run(self, host_id: str, run: dict[str, Any]) -> None:
        """Publish run and retain streamed logs across ordinary REST polls."""
        previous = self._run_cache.get(host_id)
        if previous and previous.get("id") == run.get("id"):
            run = dict(run)
            for key in ("_ha_console_output", "_ha_console_live", "_ha_console_pending",
                        "_ha_console_last_line", "_ha_console_line_number",
                        "_ha_target_packages", "_ha_before_last_update"):
                if key not in run and key in previous:
                    run[key] = previous[key]
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
        if not names:
            # Freeze the packages that actually needed updates at submission.
            # A patch-all run must not show progress for already-current Alloy,
            # etc. This snapshot survives subsequent inventory refreshes.
            packages = self._package_cache.get(host_id)
            if packages is None:
                packages = (self.host(host_id) or {}).get("_update_packages", [])
            run["_ha_target_packages"] = sorted({
                str(pkg["name"]) for pkg in packages
                if isinstance(pkg, dict) and pkg.get("name")
                and pkg.get("needs_update") is True
            })
            run["_ha_before_last_update"] = (self.host(host_id) or {}).get("last_update")
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

        # An update can finish before the 10-second status poll sees "running".
        # Subscribe now: the stream returns a snapshot even for completed runs.
        # Do not keep a socket open waiting for approval or a scheduled start.
        from homeassistant.util import dt as dt_util

        run_at = result.get("run_at")
        scheduled = None
        if isinstance(run_at, str):
            try:
                scheduled = dt_util.parse_datetime(run_at)
            except (TypeError, ValueError):
                pass
        future_run = (
            scheduled is not None
            and scheduled.tzinfo is not None
            and scheduled > dt_util.utcnow() + timedelta(seconds=5)
        )
        if not result.get("pending_approval") and not future_run:
            self._ensure_console_stream(host_id, run_id)

    def _ensure_console_stream(self, host_id: str, run_id: str) -> None:
        """One WebSocket per host/run, including runs started outside HA."""
        current = self._stream_tasks.get(host_id)
        if current and not current.done() and self._stream_run_ids.get(host_id) == run_id:
            return
        if current and not current.done():
            current.cancel()
        self._stream_run_ids[host_id] = run_id
        task = self.hass.async_create_background_task(
            self._consume_console_stream(host_id, run_id),
            f"PatchMon console stream {run_id}",
        )
        self._stream_tasks[host_id] = task
        task.add_done_callback(lambda done: self._clear_stream_task(host_id, done))

    def _clear_stream_task(self, host_id: str, task: asyncio.Task[None]) -> None:
        if self._stream_tasks.get(host_id) is task:
            self._stream_tasks.pop(host_id, None)
            self._stream_run_ids.pop(host_id, None)

    def console_run(self, host_id: str) -> dict[str, Any]:
        """Return the most recent data, including chunks not yet coordinator-flushed."""
        return self._run_cache.get(host_id) or (self.host(host_id) or {}).get("_patch_run") or {}

    def add_console_line_listener(self, host_id: str, listener):
        """Listen to individual completed lines without waiting for a status poll."""
        listeners = self._console_line_listeners.setdefault(host_id, set())
        listeners.add(listener)

        def remove() -> None:
            listeners.discard(listener)
            if not listeners:
                self._console_line_listeners.pop(host_id, None)

        return remove

    def _apply_console_event(self, host_id: str, run_id: str, event: dict[str, Any]) -> None:
        """Keep COMPLETE output and publish every newly completed console line.

        The server's snapshot is authoritative after reconnect. Only its new
        suffix is emitted as live lines, so reconnecting does not replay history.
        A chunk can contain multiple lines or a partial line.
        """
        run = self._run_cache.get(host_id)
        if not run or run.get("id") != run_id:
            return
        kind = event.get("type")
        previous = str(run.get("_ha_console_output") or "")
        pending = str(run.get("_ha_console_pending") or "")
        sequence = int(run.get("_ha_console_line_number") or 0)
        last_line = run.get("_ha_console_last_line")
        completed_lines: list[str] = []

        if kind == "snapshot":
            snapshot = event.get("shell_output")
            if not isinstance(snapshot, str):
                return
            if "_ha_console_output" not in run:
                # Initial snapshot: show the latest historic line without
                # replaying an entire existing run into HA history.
                addition = ""
                output = snapshot
                normalized = snapshot.replace("\r\n", "\n").replace("\r", "\n")
                parts = normalized.split("\n")
                pending = parts[-1]
                if len(parts) > 1:
                    sequence = len(parts) - 2
                    last_line = parts[-2]
                    completed_lines.append(last_line)
            elif snapshot.startswith(previous):
                addition = snapshot[len(previous):]
                output = snapshot
            elif previous.startswith(snapshot):
                # Persisted output may lag events we received before reconnect.
                addition = ""
                output = previous
            else:
                # First subscription or authoritative reset: prime the state
                # from the snapshot without replaying every historic line.
                addition = ""
                output = snapshot
                normalized = snapshot.replace("\r\n", "\n").replace("\r", "\n")
                parts = normalized.split("\n")
                pending = parts[-1]
                sequence = max(0, len(parts) - 2)
                if len(parts) > 1:
                    last_line = parts[-2]
                    completed_lines.append(last_line)
        elif kind == "chunk":
            addition = event.get("chunk")
            if not isinstance(addition, str) or not addition:
                return
            output = previous + addition
        elif kind == "done":
            addition = ""
            output = previous
        else:
            return

        if addition:
            # Preserve the raw full output. Normalize CR/LF only for line events.
            lines = (pending + addition.replace("\r\n", "\n").replace("\r", "\n")).split("\n")
            pending = lines.pop()
            completed_lines.extend(lines)

        if kind == "done" and pending:
            # Publish an unterminated final line as well.
            completed_lines.append(pending)
            pending = ""

        if kind == "snapshot" and event.get("stage") == "running":
            run = {**run, "status": "running"}
        if kind == "done" and isinstance(event.get("error_message"), str):
            run = {**run, "error_message": event["error_message"]}

        next_sequence = sequence + len(completed_lines)
        if completed_lines:
            last_line = completed_lines[-1]
        run = {
            **run,
            "_ha_console_output": output,
            "_ha_console_pending": pending,
            "_ha_console_last_line": last_line,
            "_ha_console_line_number": next_sequence,
            "_ha_console_live": kind != "done",
        }
        self._run_cache[host_id] = run

        for index, line in enumerate(completed_lines):
            # A distinct HA sensor state update per line (including duplicates).
            # The latest line lives in the state; the entire run remains in
            # console_output for the popup, with no arbitrary tail truncation.
            line_sequence = sequence + index + 1
            for listener in tuple(self._console_line_listeners.get(host_id, ())):
                listener(run_id, line, line_sequence)

        if kind != "chunk":
            flush = self._console_flush_tasks.pop(host_id, None)
            if flush and not flush.done():
                flush.cancel()
            self.publish_patch_run(host_id, run)
            return

        # Avoid emitting redundant coordinator updates for every partial chunk.
        # Completed lines have already notified the sensor immediately.
        existing = self._console_flush_tasks.get(host_id)
        if not existing or existing.done():
            task = self.hass.async_create_background_task(
                self._flush_console(host_id, run_id),
                f"PatchMon console flush {run_id}",
            )
            self._console_flush_tasks[host_id] = task

    async def _flush_console(self, host_id: str, run_id: str) -> None:
        await asyncio.sleep(0.75)
        current = self._run_cache.get(host_id)
        if current and current.get("id") == run_id:
            self.publish_patch_run(host_id, current)

    async def _consume_console_stream(self, host_id: str, run_id: str) -> None:
        """Reconnect with snapshots if chunks are missed; status poll remains fallback."""
        for attempt in range(4):
            try:
                async for event in self.client.async_stream_patch_run(run_id):
                    self._apply_console_event(host_id, run_id, event)
                    if event.get("type") == "done":
                        return
            except PatchMonAuthenticationError as err:
                _LOGGER.warning("PatchMon console stream auth failed for %s: %s", run_id, err)
                break
            except PatchMonError as err:
                _LOGGER.debug("PatchMon console stream %s interrupted: %s", run_id, err)

            run = self._run_cache.get(host_id)
            if not run or run.get("id") != run_id or run.get("status") not in ACTIVE_RUN_STATUSES:
                break
            await asyncio.sleep(min(2 ** attempt, 15))

        current = self._run_cache.get(host_id)
        if current and current.get("id") == run_id:
            self.publish_patch_run(host_id, {**current, "_ha_console_live": False})

    def _cancel_follow_tasks(self) -> None:
        for task in self._follow_tasks.values():
            task.cancel()
        self._follow_tasks.clear()
        for tasks in (self._stream_tasks, self._console_flush_tasks):
            for task in tasks.values():
                task.cancel()
            tasks.clear()
        self._stream_run_ids.clear()
        self._console_line_listeners.clear()

    def _clear_task(self, host_id: str, task: asyncio.Task[None]) -> None:
        if self._follow_tasks.get(host_id) is task:
            self._follow_tasks.pop(host_id, None)

    async def _refresh_one_host_packages(
        self, host_id: str, package_name: str | None,
        *, fresh_host: dict[str, Any] | None = None,
    ) -> bool:
        """Refresh host packages, optionally with fresh post-patch host metadata."""
        tracked = bool(self.watched_packages(host_id))
        packages = await self.client.async_get_packages(host_id, updates_only=not tracked)
        self._package_cache[host_id] = packages
        if self.data and host_id in self.data:
            data = dict(self.data)
            host = dict(data[host_id])
            if fresh_host is not None:
                host.update({k: v for k, v in fresh_host.items() if not k.startswith("_")})
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

    async def _fresh_post_patch_host(
        self, host_id: str, run: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Wait for an actual agent inventory report after patch-all finished."""
        hosts = await self.client.async_get_hosts(self.host_groups or None)
        host = next((h for h in hosts if str(h.get("id")) == host_id), None)
        if host is None:
            return None
        return host if post_patch_report_is_fresh(
            run.get("_ha_before_last_update"),
            host.get("last_update"), run.get("completed_at"),
        ) else None

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
            if run.get("status") == "running":
                self._ensure_console_stream(host_id, run_id)
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
        # A patch-all job must not be considered verified merely because an
        # endpoint returned the *old* package inventory. Wait for a new agent
        # report first, then fetch packages and host summary in the same pass.
        tracked_run = self.console_run(host_id)
        patch_all = tracked_run.get("patch_type") == "patch_all"
        attempts = 36 if success else 1
        confirmed = False
        for attempt in range(attempts):
            try:
                if success and patch_all:
                    fresh_host = await self._fresh_post_patch_host(host_id, tracked_run)
                    if fresh_host is not None:
                        confirmed = await self._refresh_one_host_packages(
                            host_id, None, fresh_host=fresh_host,
                        )
                else:
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
