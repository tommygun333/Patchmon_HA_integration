"""Durable, bounded security-maintenance cycle history for the PatchMon HA integration.

Stores full reports outside HA entity state/Recorder. Unit-testable with a store
implementing async_load/async_save. A checkpoint is committed before approving
an installation and after every major transition, to support safe recovery.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

RETAIN_CYCLES = 26
TERMINAL_HOSTS = frozenset({
    "verified", "no_security_updates", "blocked", "validation_failed",
    "installation_failed", "failed", "needs_attention", "unverified",
    "health_precheck_failed", "not_started", "validated", "ready",
    "interrupted_needs_review", "interrupted_installation_failed",
    "recovered_verified", "recovered_needs_attention",
})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cycle_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Compact metadata for dashboard overview; full objects stay in storage."""
    hosts = report.get("hosts", [])
    return {
        "report_id": report.get("report_id"),
        "started_at": report.get("started_at"),
        "completed_at": report.get("completed_at"),
        "mode": report.get("mode"),
        "status": report.get("status"),
        "summary": report.get("summary", {}),
        "hosts": [{
            "host_id": host.get("host_id"),
            "hostname": host.get("hostname", host.get("host_id")),
            "state": host.get("state"),
            "security_count": host.get("security_count", 0),
            "reboot_required": bool(host.get("verification", {}).get("reboot_required")
                                    or host.get("reboot_required")),
            "error": host.get("error") or host.get("reason") or "",
        } for host in hosts],
        "preflight": report.get("preflight", {}),
    }


class MaintenanceHistory:
    """Persistent reports per PatchMon config entry, newest first."""

    def __init__(self, store: Any, *, limit: int = RETAIN_CYCLES) -> None:
        self._store = store
        self.limit = max(1, limit)
        self._reports: list[dict[str, Any]] = []
        self._active: dict[str, Any] | None = None
        self._acknowledged: str | None = None
        self._listeners: list[Callable[[], None]] = []

    async def load(self) -> None:
        data = await self._store.async_load() or {}
        self._reports = sorted(
            (r for r in data.get("reports", []) if isinstance(r, dict)),
            key=lambda r: str(r.get("started_at") or ""), reverse=True,
        )[:self.limit]
        self._active = data.get("active")
        self._acknowledged = data.get("acknowledged")
        self._emit()

    def subscribe(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        def unsubscribe() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)
        return unsubscribe

    def _emit(self) -> None:
        for cb in tuple(self._listeners):
            cb()

    async def _persist(self) -> None:
        await self._store.async_save({
            "reports": self._reports, "active": self._active,
            "acknowledged": self._acknowledged,
        })
        self._emit()

    def new_id(self) -> str:
        return str(uuid4())

    @property
    def active(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._active)

    @property
    def unread(self) -> bool:
        latest = self.latest()
        return bool(latest and latest.get("report_id") != self._acknowledged)

    def latest(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._reports[0]) if self._reports else None

    def latest_host(self, host_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        for report in self._reports:
            for host in report.get("hosts", []):
                if host.get("host_id") == host_id:
                    return copy.deepcopy(report), copy.deepcopy(host)
        return None

    def list(self, *, limit: int = RETAIN_CYCLES, host_id: str | None = None,
             detailed: bool = False) -> list[dict[str, Any]]:
        reports = self._reports
        if host_id:
            reports = [r for r in reports if any(
                h.get("host_id") == host_id for h in r.get("hosts", []))]
        reports = reports[:max(1, min(limit, self.limit))]
        return copy.deepcopy(reports if detailed else [cycle_summary(r) for r in reports])

    def get(self, report_id: str) -> dict[str, Any] | None:
        if self._active and self._active.get("report_id") == report_id:
            return copy.deepcopy(self._active)
        for report in self._reports:
            if report.get("report_id") == report_id:
                return copy.deepcopy(report)
        return None

    async def start(self, report: dict[str, Any]) -> None:
        if self._active is not None:
            raise ValueError("A persisted maintenance cycle is not resolved; reconcile it first")
        if not report.get("report_id"):
            raise ValueError("Maintenance cycle requires a report_id")
        self._active = copy.deepcopy(report)
        await self._persist()

    async def checkpoint(self, report: dict[str, Any]) -> None:
        if not self._active or self._active.get("report_id") != report.get("report_id"):
            raise ValueError("Maintenance checkpoint does not match the active cycle")
        self._active = copy.deepcopy(report)
        await self._persist()

    async def finish(self, report: dict[str, Any]) -> None:
        if self._active and self._active.get("report_id") != report.get("report_id"):
            raise ValueError("Maintenance completion does not match active cycle")
        if not report.get("completed_at"):
            report = {**report, "completed_at": utc_now()}
        rid = report.get("report_id")
        self._reports = [copy.deepcopy(report)] + [
            item for item in self._reports if item.get("report_id") != rid
        ]
        self._reports = self._reports[:self.limit]
        self._active = None
        await self._persist()

    async def acknowledge(self, report_id: str | None = None) -> str | None:
        latest = self.latest()
        if not latest:
            return None
        if report_id is not None and report_id != latest.get("report_id"):
            raise ValueError("Only the latest maintenance cycle can be acknowledged")
        self._acknowledged = latest["report_id"]
        await self._persist()
        return self._acknowledged

    async def reconcile(self, client: Any, verify: Callable[..., Any]) -> dict[str, Any] | None:
        """Observe interrupted runs without triggering or approving new patch jobs.

        A crash immediately after approval can occur before the new run ID is
        checkpointed. Recover that run by its validation_run_id from PatchMon
        history, or preserve the active cycle and require manual review.
        """
        if self._active is None:
            return None
        report = copy.deepcopy(self._active)
        unresolved = False
        for host in report.get("hosts", []):
            state = host.get("state")
            install = host.get("installation") or {}
            run_id = install.get("run_id")
            validation_id = (host.get("validation") or {}).get("run_id")
            if not run_id and state == "approving":
                try:
                    recent = await client.async_get_patch_runs(host["host_id"], limit=50)
                    matches = [r for r in recent if r.get("validation_run_id") == validation_id
                               and r.get("id")]
                    if len(matches) == 1:
                        run_id = matches[0]["id"]
                        install.update({"run_id": run_id, "validation_run_id": validation_id})
                        host["installation"] = install
                    else:
                        host["state"] = "recovery_needs_manual_review"
                        host["error"] = "Approval outcome uncertain; check PatchMon run history"
                        unresolved = True
                        continue
                except Exception as exc:
                    host["state"] = "recovery_unavailable"
                    host["error"] = str(exc)[:400]
                    unresolved = True
                    continue
            if not run_id:
                if state in ("validating", "validated", "ready", "running"):
                    host["state"] = "interrupted_before_install"
                continue
            try:
                actual = await client.async_get_patch_run(run_id)
                if actual.get("id") not in (None, run_id):
                    raise ValueError("PatchMon returned a different run ID")
                status = actual.get("status")
                install["status"] = status
                host["installation"] = install
                if status == "completed":
                    names = [p["name"] for p in host.get("security_packages", [])]
                    check = await verify(client, host["host_id"], host.get("last_report"), names)
                    host["verification"] = check
                    # Post-restart we can verify package inventory, but HA was
                    # unavailable to run scheduled service-health checks.
                    # Never claim full recovery success on that basis alone.
                    host["state"] = "recovered_needs_attention"
                    host["recovery_note"] = (
                        "Package inventory checked after restart; verify service health manually"
                    )
                elif status in ("failed", "cancelled", "timed_out", "agent_disconnected"):
                    host["state"] = "interrupted_installation_failed"
                    host["error"] = actual.get("error_message") or "PatchMon installation failed"
                else:
                    host["state"] = "installing"
                    unresolved = True
            except Exception as exc:
                host["state"] = "recovery_unavailable"
                host["error"] = str(exc)[:400]
                unresolved = True
        known_ids = {h.get("host_id") for h in report.get("hosts", [])}
        for host_id in report.get("requested_host_ids", []):
            if host_id not in known_ids:
                report.setdefault("hosts", []).append({
                    "host_id": host_id, "state": "not_started",
                    "reason": "Home Assistant restarted before this host was processed",
                })
        report["status"] = "interrupted_running" if unresolved else "interrupted_needs_attention"
        report["recovery_checked_at"] = utc_now()
        report["summary"] = {
            "hosts": len(report.get("hosts", [])),
            "verified": sum(h.get("state") in ("verified", "recovered_verified") for h in report.get("hosts", [])),
            "failed": sum(h.get("state") not in ("verified", "recovered_verified", "no_security_updates") for h in report.get("hosts", [])),
            "reboot_required": sum(bool(h.get("verification", {}).get("reboot_required") or h.get("reboot_required"))
                                   for h in report.get("hosts", [])),
        }
        if unresolved:
            await self.checkpoint(report)
        else:
            report["completed_at"] = utc_now()
            await self.finish(report)
        return report
