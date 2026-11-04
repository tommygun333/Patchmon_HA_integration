"""Security-update selection and native PatchMon validation helpers.

An eligible security flag selects the *initial package name*. The agent may
install newer candidate versions and additional dependencies, by design.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

# The upstream PatchMon patch_package endpoint has a 100-package limit.
MAX_PACKAGES_PER_VALIDATION = 100
PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+_.:-]{0,199}$")
VALIDATION_OK = frozenset({"validated"})
VALIDATION_TERMINAL = VALIDATION_OK | frozenset(
    {"completed", "approved", "failed", "cancelled", "timed_out", "agent_disconnected"}
)


def _report_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    return when.astimezone(timezone.utc)


def prepare_security_plan(
    host_id: str,
    host: dict[str, Any] | None,
    pending_updates: list[dict[str, Any]],
    *,
    max_report_age_minutes: int = 120,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return an auditable plan without scheduling or installing anything.

    Only explicit security-flagged names are selected as primary targets.
    Dependencies and later versions may be installed by the package manager.
    """
    host = host or {}
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must have a timezone")
    now = now.astimezone(timezone.utc)
    errors: list[str] = []
    report_at = _report_time(host.get("last_update"))
    if report_at is None:
        errors.append("Host has no valid, timezone-aware last report timestamp")
    elif report_at > now + timedelta(minutes=5):
        errors.append("Host report timestamp is in the future")
    elif now - report_at > timedelta(minutes=max_report_age_minutes):
        errors.append("Host inventory is stale")

    if host.get("effective_status") in ("inactive", "offline"):
        errors.append("PatchMon reports the host offline")
    if not host:
        errors.append("Host is missing from the current PatchMon inventory")

    selected: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for package in pending_updates:
        if package.get("is_security_update") is not True:
            continue
        name = package.get("name")
        if not isinstance(name, str) or not PACKAGE_NAME.fullmatch(name):
            errors.append("Security-flagged update has an invalid package name")
            continue
        if name in seen:
            errors.append(f"Duplicate security package in inventory: {name}")
            continue
        seen.add(name)
        selected.append({
            "name": name,
            "installed_version": package.get("current_version"),
            "available_version": package.get("available_version"),
        })
    selected.sort(key=lambda package: str(package["name"]))

    if len(selected) > MAX_PACKAGES_PER_VALIDATION:
        errors.append("Too many security packages for one PatchMon validation run")
    reported_security = host.get("security_updates_count")
    if reported_security is not None:
        try:
            if int(reported_security) != len(selected):
                errors.append("Host security count differs from its live package inventory")
        except (TypeError, ValueError):
            errors.append("Invalid reported security update count")

    state = "blocked" if errors else ("ready" if selected else "no_security_updates")
    return {
        "host_id": host_id,
        "hostname": host.get("hostname") or host_id,
        "state": state,
        "validation_eligible": state == "ready",
        "execution_safe": False,
        "security_packages": selected,
        "security_count": len(selected),
        "regular_count": max(0, len(pending_updates) - len(selected)),
        "last_report": report_at.isoformat() if report_at else None,
        "reboot_required": bool(host.get("needs_reboot")),
        "errors": errors,
        "execution_note": (
            "Package selection is security-flagged, not repository-restricted. "
            "PatchMon may install newer versions and required dependencies."
        ),
    }


def _extra_packages(run: dict[str, Any], selected: set[str]) -> list[str]:
    affected = run.get("packages_affected")
    if not isinstance(affected, list):
        return []
    names: set[str] = set()
    for item in affected:
        name = item if isinstance(item, str) else item.get("name") if isinstance(item, dict) else None
        if isinstance(name, str) and name not in selected:
            names.add(name)
    return sorted(names)


async def validate_security_plan(
    client: Any,
    plan: dict[str, Any],
    *,
    timeout_seconds: int = 1800,
    poll_seconds: int = 5,
) -> dict[str, Any]:
    """Simulate an explicitly selected list using the PatchMon agent."""
    if plan["state"] != "ready":
        return {"status": "skipped", "reason": plan["state"], "success": False}

    names = [str(package["name"]) for package in plan["security_packages"]]
    if not names or len(names) > MAX_PACKAGES_PER_VALIDATION:
        return {"status": "blocked", "reason": "Invalid validation package count", "success": False}
    queued = await client.async_patch_host(
        plan["host_id"], package_names=names, dry_run=True,
        pending_approval=False, run_immediately=True,
    )
    run_id = queued.get("patch_run_id")
    if not isinstance(run_id, str) or not run_id:
        return {"status": "failed", "reason": "PatchMon returned no patch_run_id", "success": False}

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        run = await client.async_get_patch_run(run_id)
        if run.get("id") not in (None, run_id):
            return {"status": "failed", "success": False, "run_id": run_id,
                    "reason": "PatchMon returned a different run"}
        status = str(run.get("status") or "unknown")
        if status in VALIDATION_TERMINAL:
            extras = _extra_packages(run, set(names))
            removals = apt_removals(run.get("shell_output"))
            return {
                "status": status,
                "success": status in VALIDATION_OK and not removals,
                "run_id": run_id,
                "extra_dependencies": extras,
                "removals": removals,
                "execution_safe": False,
                "reason": (str(run.get("error_message") or "") or
                           ("Simulation proposes package removals" if removals else ""))[:400],
            }
        remaining = deadline - loop.time()
        if remaining <= 0:
            return {
                "status": "validation_wait_timeout",
                "success": False,
                "run_id": run_id,
                "execution_safe": False,
                "reason": "PatchMon run is still pending; no packages were installed by this action",
            }
        await asyncio.sleep(min(poll_seconds, remaining))


def apt_removals(shell_output: Any) -> list[str]:
    """Conservatively flag APT's `-s` removals (preflight policy: fail closed)."""
    if not isinstance(shell_output, str):
        return []
    found = []
    for line in shell_output.splitlines():
        if line.startswith("Remv "):
            name = line.split()[1] if len(line.split()) > 1 else "unknown"
            found.append(name)
    return sorted(set(found))
