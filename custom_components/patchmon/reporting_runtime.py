"""Home Assistant services and lifecycle for persistent PatchMon security reports."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store

from .maintenance_history import MaintenanceHistory, utc_now
from .maintenance_workflow import (
    run_security_maintenance,
    validate_pending_updates,
    verify_post_patch,
)

_LOGGER = logging.getLogger(__name__)
EVENT_REPORT = "patchmon_security_maintenance_report"
EVENT_PROGRESS = "patchmon_security_maintenance_progress"

REPORTS_SCHEMA = vol.Schema({
    vol.Optional("config_entry_id"): cv.string,
    vol.Optional("report_id"): cv.string,
    vol.Optional("host_id"): cv.string,
    vol.Optional("report_source"): vol.In(("automation", "manual")),
    vol.Optional("limit", default=26): vol.All(vol.Coerce(int), vol.Range(min=1, max=26)),
})
ACK_SCHEMA = vol.Schema({
    vol.Optional("config_entry_id"): cv.string,
    vol.Optional("report_id"): cv.string,
})
RECONCILE_SCHEMA = vol.Schema({vol.Optional("config_entry_id"): cv.string})
RESOLVE_SCHEMA = vol.Schema({
    vol.Optional("config_entry_id"): cv.string,
    vol.Required("confirm_no_active_patch_runs"): cv.boolean,
})
BACKUP_CHECK = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
    vol.Optional("expected_state", default="on"): cv.string,
    vol.Required("timestamp_attribute"): cv.string,
    vol.Optional("max_age_hours", default=30): vol.All(vol.Coerce(int), vol.Range(min=1, max=168)),
})
ACTIVE_BACKUP_CHECK = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
    vol.Optional("expected_state", default="off"): cv.string,
})
HEALTH_CHECK = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
    vol.Optional("expected_state", default="on"): cv.string,
})


def extend_maintenance_schema(original: vol.Schema) -> vol.Schema:
    """Add optional safety checks without changing existing manual calls."""
    schema = dict(original.schema)
    schema.update({
        vol.Optional("require_backup_checks", default=False): cv.boolean,
        vol.Optional("backup_checks", default=[]): vol.All(cv.ensure_list, [BACKUP_CHECK]),
        vol.Optional("backup_idle_checks", default=[]): vol.All(cv.ensure_list, [ACTIVE_BACKUP_CHECK]),
        vol.Optional("health_checks", default={}): {cv.string: vol.All(cv.ensure_list, [HEALTH_CHECK])},
        vol.Optional("health_timeout_seconds", default=120): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=600)),
    })
    return vol.Schema(schema)


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return result.astimezone(timezone.utc) if result.tzinfo else None


def backup_preflight(hass: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Fail closed on required latest-success and backup-in-progress signals."""
    checks = config.get("backup_checks", [])
    idle = config.get("backup_idle_checks", [])
    required = config.get("require_backup_checks", False)
    failures = []
    details = []
    if required and (not checks or not idle):
        failures.append("Backup success AND backup-idle checks must both be configured")
    for item in checks:
        entity = hass.states.get(item["entity_id"])
        if entity is None or entity.state != item.get("expected_state", "on"):
            failures.append(f"Backup success not confirmed: {item['entity_id']}")
            continue
        attribute = item.get("timestamp_attribute")
        # Do not rely on last_changed: HA startup could make an old backup
        # appear recent. Only an explicit backup-success timestamp is trusted.
        stamp = entity.attributes.get(attribute) if attribute else None
        when = _parse_timestamp(stamp)
        age = item.get("max_age_hours", 30)
        if when is None or when > datetime.now(timezone.utc) or (
            datetime.now(timezone.utc) - when
        ).total_seconds() > age * 3600:
            failures.append(f"No recent successful backup timestamp: {item['entity_id']}")
            continue
        details.append({"entity_id": item["entity_id"], "last_success": when.isoformat()})
    for item in idle:
        entity = hass.states.get(item["entity_id"])
        if entity is None or entity.state != item.get("expected_state", "off"):
            failures.append(f"Backup still running or idle state unknown: {item['entity_id']}")
    return {"status": "passed" if not failures else "blocked", "failures": failures, "successes": details,
            "configured": bool(checks and idle)}


async def check_host_health(hass: Any, checks: dict[str, Any], host_id: str,
                            phase: str, timeout: int = 120) -> dict[str, Any]:
    """Compare HA/Uptime Kuma entity states; only configured checks count."""
    entries = checks.get(host_id, [])
    if not entries:
        return {"status": "not_configured", "phase": phase, "checked": 0}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (timeout if phase == "after" else 0)
    while True:
        failures = []
        states = []
        for item in entries:
            entity = hass.states.get(item["entity_id"])
            actual = entity.state if entity else "unavailable"
            expected = item.get("expected_state", "on")
            states.append({"entity_id": item["entity_id"], "expected": expected, "actual": actual})
            if actual != expected:
                failures.append(item["entity_id"])
        if not failures:
            return {"status": "passed", "phase": phase, "checked": len(entries), "entities": states}
        if loop.time() >= deadline:
            return {"status": "failed", "phase": phase, "checked": len(entries),
                    "entities": states, "failed_entities": failures}
        await asyncio.sleep(min(10, deadline - loop.time()))


async def setup_reporting(hass: Any, coordinator: Any) -> MaintenanceHistory:
    """Load history before platform setup; register the report viewer resource."""
    history = MaintenanceHistory(Store(hass, 1, f"patchmon_security_cycles_{coordinator.entry.entry_id}"))
    await history.load()
    coordinator.maintenance_reports = history
    if history.active:
        # Reconcile existing PatchMon run IDs; never reapprove or start a new job.
        async def recover() -> None:
            for _ in range(120):  # up to 60 minutes if a patch is still running
                if not history.active:
                    return
                try:
                    result = await history.reconcile(coordinator.client, verify_post_patch)
                    if result and not history.active:
                        hass.bus.async_fire(EVENT_REPORT, {
                            "report_id": result.get("report_id"), "status": result.get("status"),
                            "report_source": result.get("report_source", "automation"),
                            "recovered": True, "summary": result.get("summary", {}),
                        })
                        if result.get("report_source") == "automation":
                            _notify(hass, result)
                        return
                except Exception:
                    _LOGGER.exception("Cannot reconcile interrupted PatchMon security cycle")
                await asyncio.sleep(30)
        hass.async_create_background_task(recover(), "PatchMon security recovery")
    return history


def _summary_event(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_id": report.get("report_id"), "started_at": report.get("started_at"),
        "report_source": report.get("report_source", "automation"),
        "report_type": report.get("report_type", "security_maintenance"),
        "completed_at": report.get("completed_at"), "mode": report.get("mode"),
        "status": report.get("status"), "summary": report.get("summary", {}),
        "hosts": [{"host_id": h.get("host_id"), "state": h.get("state"),
                   "validation_run_id": h.get("validation", {}).get("run_id"),
                   "installation_run_id": h.get("installation", {}).get("run_id")}
                  for h in report.get("hosts", [])],
    }


def report_url(report: dict[str, Any]) -> str:
    """Stable hidden-panel route for one exact config entry and report ID."""
    entry_id = quote(str(report.get("config_entry_id") or ""), safe="")
    report_id = quote(str(report.get("report_id") or ""), safe="")
    return f"/patchmon-reports/{entry_id}/{report_id}"


def _notify(hass: Any, report: dict[str, Any]) -> None:
    s = report.get("summary", {})
    manual_validation = report.get("report_type") == "patch_all_validation"
    lines = [
        ("**PatchMon manual update validation**" if manual_validation else
         f"**PatchMon security maintenance — {report.get('mode')}**"),
        f"Status: **{report.get('status')}** | Date: {report.get('completed_at')}",
        f"Hosts: {s.get('hosts', 0)} | Verified: {s.get('verified', 0)} | "
        f"Failed: {s.get('failed', 0)} | Reboot required: {s.get('reboot_required', 0)}",
    ]
    for h in report.get("hosts", []):
        lines.append(f"- {h.get('hostname') or h.get('host_id')}: {h.get('state')} "
                     f"({h.get('security_count', 0)} security updates)")
    lines.append(f"[View report]({report_url(report)})")
    entry_id = str(report.get("config_entry_id") or "unknown")
    report_id = str(report.get("report_id") or "unknown")
    persistent_notification.async_create(
        hass, "\n".join(lines),
        title=("PatchMon manual update validation" if manual_validation
               else "PatchMon security maintenance"),
        notification_id=f"patchmon_report_{entry_id}_{report_id}",
    )


async def run_reporting_maintenance(hass: Any, coordinator: Any,
                                    data: dict[str, Any]) -> dict[str, Any]:
    """HA orchestration around upstream patch-run lifecycle + persistent report."""
    mode = data.get("mode") or ("validate" if data.get("validate", True) else "plan")
    report_source = data.get("report_source", "automation")
    history: MaintenanceHistory = coordinator.maintenance_reports
    if mode == "install" and history.active:
        raise ServiceValidationError(
            "Previous cycle may have an active PatchMon installation. "
            "Reconcile it before starting another installation."
        )
    if mode == "install":
        preflight = backup_preflight(hass, data)
        if data.get("require_backup_checks") and preflight["status"] != "passed":
            report = {
                "report_id": history.new_id(), "report_source": report_source,
                "report_type": "security_maintenance",
                "mode": mode, "status": "blocked",
                "started_at": utc_now(), "completed_at": utc_now(),
                "preflight": preflight,
                "hosts": [{"host_id": id_, "state": "not_started", "reason": "Backup gate blocked"}
                          for id_ in data["host_ids"]],
                "summary": {"hosts": len(data["host_ids"]), "verified": 0, "validated": 0,
                            "planned": 0, "no_security_updates": 0, "blocked": 1,
                            "failed": 1, "not_started": len(data["host_ids"]),
                            "reboot_required": 0},
            }
            report["config_entry_id"] = coordinator.entry.entry_id
            await history.finish(report)
            hass.bus.async_fire(EVENT_REPORT, _summary_event(report))
            if report_source == "automation" or data.get("notify", True):
                _notify(hass, report)
            return report

    report_id = history.new_id()
    started = False

    async def checkpoint(report: dict[str, Any]) -> None:
        nonlocal started
        # Retain backup evidence and config ID in *each* crash-safe checkpoint.
        report.setdefault("preflight", preflight)
        report.setdefault("config_entry_id", coordinator.entry.entry_id)
        report.setdefault("report_source", report_source)
        report.setdefault("report_type", "security_maintenance")
        if not started:
            await history.start(report)
            started = True
        else:
            await history.checkpoint(report)

    def progress(entry: dict[str, Any]) -> None:
        hass.bus.async_fire(EVENT_PROGRESS, {
            "report_id": report_id, "host_id": entry.get("host_id"),
            "state": entry.get("state"),
            "validation_run_id": entry.get("validation", {}).get("run_id"),
            "installation_run_id": entry.get("installation", {}).get("run_id"),
        })
        installation_id = entry.get("installation", {}).get("run_id")
        if entry.get("state") == "installing" and installation_id:
            followed = hass.data.setdefault("patchmon", {}).setdefault("followed_runs", set())
            if installation_id not in followed:
                followed.add(installation_id)
                coordinator.follow_patch(entry["host_id"],
                    [p["name"] for p in entry.get("security_packages", [])],
                    {"patch_run_id": installation_id})

    async def health_check(host_id: str, phase: str) -> dict[str, Any]:
        return await check_host_health(
            hass, data.get("health_checks", {}), host_id, phase,
            timeout=data.get("health_timeout_seconds", 120),
        )

    try:
        report = await run_security_maintenance(
            coordinator.client, data["host_ids"], mode=mode,
            max_report_age_minutes=data.get("max_report_age_minutes", 120),
            stop_on_failure=data.get("stop_on_failure", True),
            report_id=report_id, checkpoint=checkpoint if mode == "install" else None,
            progress=progress, health_check=health_check if mode == "install" else None,
        )
    except BaseException:
        # Keep the durable, potentially in-flight checkpoint for reconciliation.
        _LOGGER.exception("Security maintenance interrupted; persisted checkpoint retained")
        raise

    report["config_entry_id"] = coordinator.entry.entry_id
    report["report_source"] = report_source
    report["report_type"] = "security_maintenance"
    if mode == "install":
        report["preflight"] = preflight
    await history.finish(report)
    hass.bus.async_fire(EVENT_REPORT, _summary_event(report))
    if report_source == "automation" or data.get("notify", True):
        _notify(hass, report)
    return report


async def run_reporting_validation(
    hass: Any, coordinator: Any, data: dict[str, Any],
) -> dict[str, Any]:
    """Persist one manual selected-package validation without approving installs."""
    started_at = utc_now()
    host_id = data["host_id"]
    try:
        result = await validate_pending_updates(
            coordinator.client, host_id,
            max_report_age_minutes=data.get("max_report_age_minutes", 120),
        )
    except Exception as exc:
        _LOGGER.exception("PatchMon manual validation failed")
        result = {
            "host_id": host_id,
            "hostname": host_id,
            "status": "failed",
            "success": False,
            "selected_count": 0,
            "primary_packages": [],
            "reason": str(exc)[:500],
        }

    status = str(result.get("status") or "failed")
    success = bool(result.get("success")) or status == "no_updates"
    validation = dict(result.get("validation") or {})
    if result.get("reason") and not validation.get("reason"):
        validation["reason"] = str(result["reason"])[:500]
    host_report = {
        "host_id": host_id,
        "hostname": result.get("hostname") or host_id,
        "state": status,
        "security_count": result.get("selected_count", 0),
        "selected_count": result.get("selected_count", 0),
        "security_packages": result.get("package_details") or [
            {"name": name} for name in result.get("primary_packages", [])
        ],
        "validation": validation,
        "error": "" if success else str(
            result.get("reason") or validation.get("reason") or ""
        )[:500],
    }
    report = {
        "report_id": coordinator.maintenance_reports.new_id(),
        "config_entry_id": coordinator.entry.entry_id,
        "report_source": "manual",
        "report_type": "patch_all_validation",
        "mode": "validate_patch_all",
        "status": "completed" if success else "needs_attention",
        "started_at": started_at,
        "completed_at": utc_now(),
        "hosts": [host_report],
        "summary": {
            "hosts": 1,
            "verified": 0,
            "validated": 1 if result.get("success") else 0,
            "planned": 0,
            "no_security_updates": 1 if status == "no_updates" else 0,
            "blocked": 1 if status == "blocked" else 0,
            "failed": 0 if success else 1,
            "not_started": 0,
            "reboot_required": 0,
        },
        "note": result.get("note"),
    }
    await coordinator.maintenance_reports.finish(report)
    hass.bus.async_fire(EVENT_REPORT, _summary_event(report))
    if data.get("notify", True):
        _notify(hass, report)
    return {
        **result,
        "report_id": report["report_id"],
        "report_source": "manual",
        "report_url": report_url(report),
    }


def register_report_services(hass: Any, resolve: Callable[[Any, Any], Any]) -> None:
    async def get_reports(call: Any) -> dict[str, Any]:
        history = resolve(hass, call).maintenance_reports
        rid = call.data.get("report_id")
        if rid:
            report = history.get(rid)
            if report is None:
                raise ServiceValidationError("No saved security report with that ID")
            return {"report": report, "unread": history.unread,
                    "automation_unread": history.automation_unread}
        return {"reports": history.list(limit=call.data["limit"],
                                         host_id=call.data.get("host_id"),
                                         report_source=call.data.get("report_source")),
                "unread": history.unread,
                "automation_unread": history.automation_unread,
                "retained": history.limit,
                "retained_per_source": history.limit,
                "active": (history.active and {
                    "report_id": history.active.get("report_id"),
                    "status": history.active.get("status"),
                })}

    async def acknowledge(call: Any) -> dict[str, Any]:
        history = resolve(hass, call).maintenance_reports
        try:
            rid = await history.acknowledge(call.data.get("report_id"))
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc
        return {"acknowledged_report_id": rid, "unread": history.unread}

    async def acknowledge_automation(call: Any) -> dict[str, Any]:
        history = resolve(hass, call).maintenance_reports
        try:
            rid = await history.acknowledge_automation(call.data.get("report_id"))
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc
        return {"acknowledged_report_id": rid,
                "automation_unread": history.automation_unread}

    async def reconcile(call: Any) -> dict[str, Any]:
        coordinator = resolve(hass, call)
        result = await coordinator.maintenance_reports.reconcile(
            coordinator.client, verify_post_patch)
        return {"report": result, "active": bool(coordinator.maintenance_reports.active)}

    async def resolve_interrupted(call: Any) -> dict[str, Any]:
        coordinator = resolve(hass, call)
        history = coordinator.maintenance_reports
        if call.data.get("confirm_no_active_patch_runs") is not True:
            raise ServiceValidationError("Explicit confirmation is required")
        active = history.active
        if active is None:
            return {"status": "no_active_cycle"}
        # Check known and recently created runs before allowing a human to
        # close an ambiguous cycle, especially the crash-after-approval gap.
        busy = {"queued", "scheduled", "running", "pending_validation", "approved", "installing"}
        for host in active.get("hosts", []):
            run_id = (host.get("installation") or {}).get("run_id")
            if run_id:
                run = await coordinator.client.async_get_patch_run(run_id)
                if run.get("status") not in (
                    "completed", "failed", "cancelled", "timed_out", "agent_disconnected"):
                    raise ServiceValidationError(f"Patch run {run_id} is still active or unknown")
            recent = await coordinator.client.async_get_patch_runs(host["host_id"], limit=50)
            for run in recent:
                if run.get("status") in busy:
                    raise ServiceValidationError(
                        f"Host {host['host_id']} still has active run {run.get('id')}"
                    )
        active["status"] = "interrupted_needs_attention"
        active["completed_at"] = utc_now()
        await history.finish(active)
        return {"status": "resolved_with_attention", "report_id": active["report_id"]}

    for name, fn, schema in (
        ("get_security_reports", get_reports, REPORTS_SCHEMA),
        ("acknowledge_security_report", acknowledge, ACK_SCHEMA),
        ("acknowledge_automation_security_report", acknowledge_automation, ACK_SCHEMA),
        ("reconcile_security_reports", reconcile, RECONCILE_SCHEMA),
        ("resolve_interrupted_security_cycle", resolve_interrupted, RESOLVE_SCHEMA),
    ):
        hass.services.async_register("patchmon", name, fn,
                                     schema=schema, supports_response=SupportsResponse.ONLY)
