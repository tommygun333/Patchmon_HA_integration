"""HA-side PatchMon maintenance orchestration, without a PatchMon server fork.

Security automation uses the upstream selected-package validation -> approve API.
Manual patch-all preflight simulates selected pending packages; it is NOT a
native PatchMon patch_all dry-run and must not be described as equivalent.
No Home Assistant dependency here, so the state machine is unit-testable.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .security_maintenance import (
    MAX_PACKAGES_PER_VALIDATION,
    PACKAGE_NAME,
    _report_time,
    prepare_security_plan,
    validate_security_plan,
)

FAILED = frozenset({"failed", "cancelled", "timed_out", "agent_disconnected"})
INSTALL_TERMINAL = FAILED | frozenset({"completed"})


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_id(reply: dict[str, Any]) -> str:
    value = reply.get("patch_run_id")
    if not isinstance(value, str) or not value:
        raise ValueError("PatchMon returned no patch_run_id")
    return value


async def wait_for_status(
    client: Any, run_id: str, terminal: frozenset[str], *,
    timeout_seconds: int = 1800, poll_seconds: float = 5.0,
) -> dict[str, Any]:
    """Wait for one exact run, never infer success from another host run."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        run = await client.async_get_patch_run(run_id)
        if run.get("id") not in (None, run_id):
            raise ValueError("PatchMon returned an unrelated run")
        if str(run.get("status")) in terminal:
            return run
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"PatchMon run {run_id} did not finish in time")
        await asyncio.sleep(min(poll_seconds, remaining))


def validation_is_approvable(
    run: dict[str, Any], *, patch_type: str, names: list[str] | None = None,
) -> bool:
    """Do not approve pending_approval/pending_validation or mismatched runs."""
    if not (run.get("status") == "validated" and run.get("dry_run") is True
            and run.get("patch_type") == patch_type and bool(run.get("id"))):
        return False
    if names is not None:
        got = run.get("package_names")
        if not isinstance(got, list) or sorted(got) != sorted(names):
            return False
    return True


async def approve_validated_run(
    client: Any, run_id: str, *, patch_type: str, names: list[str],
) -> dict[str, Any]:
    """Approve an actual completed dry-run, preserving its package selection."""
    run = await client.async_get_patch_run(run_id)
    if run.get("id") not in (None, run_id) or not validation_is_approvable(
        run, patch_type=patch_type, names=names
    ):
        raise ValueError("PatchMon validation is not completed or its targets changed")
    approved = await client.async_approve_patch_run(run_id, run_immediately=True)
    if approved.get("validation_run_id") not in (None, run_id):
        raise ValueError("PatchMon approved a different validation run")
    return approved


async def verify_post_patch(
    client: Any, host_id: str, before_report: str | None, selected_names: list[str], *,
    timeout_seconds: int = 300, poll_seconds: float = 6.0,
) -> dict[str, Any]:
    """Require a fresh agent inventory after install, then check selected targets."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    before = _report_time(before_report)
    while True:
        hosts = await client.async_get_hosts()
        host = next((h for h in hosts if str(h.get("id")) == host_id), None)
        if host is not None:
            after = _report_time(host.get("last_update"))
            if after is not None and (before is None or after > before):
                pending = await client.async_get_updates(host_id)
                remaining = sorted({str(p["name"]) for p in pending
                                    if p.get("name") in selected_names
                                    and p.get("needs_update", True)})
                online = host.get("effective_status") not in ("offline", "inactive")
                return {
                    "status": "verified" if not remaining and online else "needs_attention",
                    "remaining_selected_packages": remaining,
                    "reboot_required": bool(host.get("needs_reboot")),
                    "host_status": host.get("effective_status"),
                    "post_report_at": host.get("last_update"),
                }
        remaining_time = deadline - loop.time()
        if remaining_time <= 0:
            return {"status": "unverified", "reason": "No fresh PatchMon report after installation"}
        await asyncio.sleep(min(poll_seconds, remaining_time))


async def run_security_maintenance(
    client: Any, host_ids: list[str], *, mode: str = "validate",
    max_report_age_minutes: int = 120, stop_on_failure: bool = True,
    validation_timeout_seconds: int = 1800, install_timeout_seconds: int = 1800,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Sequential hosts: plan -> native dry-run -> optionally approve -> verify.

    ``mode='install'`` is the ONLY path that can approve a validation.
    Hosts are processed in caller-provided order; Proxmox should be first.
    """
    if mode not in {"plan", "validate", "install"}:
        raise ValueError("Unsupported maintenance mode")
    if not host_ids or len(host_ids) > 30 or len(set(host_ids)) != len(host_ids):
        raise ValueError("Supply 1-30 unique host IDs")

    report: dict[str, Any] = {
        "started_at": _utc(), "mode": mode, "hosts": [], "summary": {},
        "selection_policy": "security-flagged primary packages; dependency upgrades allowed",
    }
    aborted = False
    for host_id in host_ids:
        if aborted:
            report["hosts"].append({"host_id": host_id, "state": "not_started", "reason": "Previous host failed"})
            continue
        entry: dict[str, Any] = {"host_id": host_id, "state": "running"}
        report["hosts"].append(entry)
        try:
            hosts = await client.async_get_hosts()
            host = next((h for h in hosts if str(h.get("id")) == host_id), None)
            if host is None:
                raise ValueError("Host missing from PatchMon inventory")
            packages = await client.async_get_updates(host_id)
            plan = prepare_security_plan(host_id, host, packages,
                                         max_report_age_minutes=max_report_age_minutes)
            entry.update(plan)
            if plan["state"] != "ready":
                if plan["state"] == "blocked":
                    aborted = stop_on_failure
                continue
            if mode == "plan":
                continue

            validation = await validate_security_plan(
                client, plan, timeout_seconds=validation_timeout_seconds,
            )
            entry["validation"] = validation
            if not validation.get("success"):
                entry["state"] = "validation_failed"
                aborted = stop_on_failure
                continue
            entry["state"] = "validated"
            if mode == "validate":
                continue

            names = [p["name"] for p in plan["security_packages"]]
            validation_id = validation.get("run_id")
            if not isinstance(validation_id, str) or not validation_id:
                raise ValueError("Validation completed without a run ID")
            approved = await approve_validated_run(
                client, validation_id, patch_type="patch_package", names=names,
            )
            installation_id = _run_id(approved)
            entry["installation"] = {
                "run_id": installation_id,
                "validation_run_id": validation_id,
                "status": "running",
            }
            entry["state"] = "installing"
            if progress:
                progress(entry)
            completed = await wait_for_status(
                client, installation_id, INSTALL_TERMINAL,
                timeout_seconds=install_timeout_seconds,
            )
            entry["installation"].update({
                "status": completed.get("status"),
                "error": completed.get("error_message"),
                "packages_affected": completed.get("packages_affected"),
            })
            if completed.get("status") != "completed":
                entry["state"] = "installation_failed"
                aborted = stop_on_failure
                continue
            entry["verification"] = await verify_post_patch(
                client, host_id, plan.get("last_report"), names,
            )
            entry["state"] = entry["verification"]["status"]
            if entry["state"] != "verified":
                aborted = stop_on_failure
        except Exception as exc:
            entry["state"] = "failed"
            entry["error"] = str(exc)[:500]
            aborted = stop_on_failure
        finally:
            if progress:
                progress(entry)

    states = [h["state"] for h in report["hosts"]]
    failures = {"blocked", "validation_failed", "installation_failed", "failed",
                "needs_attention", "unverified"}
    report["summary"] = {
        "hosts": len(states), "verified": states.count("verified"),
        "validated": states.count("validated"),
        "planned": states.count("ready"),
        "no_security_updates": states.count("no_security_updates"),
        "blocked": states.count("blocked"),
        "failed": sum(s in failures for s in states),
        "not_started": states.count("not_started"),
        "reboot_required": sum(bool(h.get("verification", {}).get("reboot_required")) for h in report["hosts"]),
    }
    report["completed_at"] = _utc()
    report["status"] = "needs_attention" if report["summary"]["failed"] or report["summary"]["not_started"] else "completed"
    return report


async def validate_pending_updates(
    client: Any, host_id: str, *, max_report_age_minutes: int = 120,
    validation_timeout_seconds: int = 1800,
) -> dict[str, Any]:
    """Manual preflight of all *currently pending names*, NOT patch_all.

    Upstream Linux patch_all dry_run is rejected. This uses patch_package with
    the pending names instead. A subsequent normal Patch all is a separate run.
    """
    hosts = await client.async_get_hosts()
    host = next((h for h in hosts if str(h.get("id")) == host_id), None)
    if host is None:
        return {"status": "blocked", "reason": "Host not in PatchMon inventory", "host_id": host_id}
    last = _report_time(host.get("last_update"))
    if (last is None or last > datetime.now(timezone.utc) + timedelta(minutes=5)
            or datetime.now(timezone.utc) - last > timedelta(minutes=max_report_age_minutes)):
        return {"status": "blocked", "reason": "Host inventory stale or invalid", "host_id": host_id}
    if host.get("effective_status") in ("inactive", "offline"):
        return {"status": "blocked", "reason": "Host offline", "host_id": host_id}
    packages = await client.async_get_updates(host_id)
    names = [p.get("name") for p in packages if p.get("needs_update", True)]
    if not names:
        return {"status": "no_updates", "host_id": host_id, "selected_count": 0}
    if (len(names) > MAX_PACKAGES_PER_VALIDATION
            or len(set(names)) != len(names)
            or any(not isinstance(name, str) or not PACKAGE_NAME.fullmatch(name) for name in names)):
        return {"status": "blocked", "host_id": host_id,
                "reason": "Invalid, duplicate, or more than 100 pending package names"}
    plan = {
        "state": "ready", "host_id": host_id,
        "security_packages": [{"name": name} for name in sorted(names)],
    }
    validation = await validate_security_plan(
        client, plan, timeout_seconds=validation_timeout_seconds,
    )
    return {
        "host_id": host_id,
        "status": validation["status"],
        "success": validation.get("success", False),
        "selected_count": len(names),
        "primary_packages": sorted(names),
        "validation": validation,
        "full_patch_all_simulation": False,
        "note": "Selected-package preflight; a normal Patch all uses a different APT upgrade transaction",
    }
