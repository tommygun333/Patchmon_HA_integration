"""Select explicit, ordered hosts for unattended PatchMon maintenance.

The saved opt-in list is separate from PatchMon inventory and package watchlists.
No newly discovered host is ever automatically added to an install run.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

MAINTENANCE_STAGES = ("proxmox", "standard", "critical", "pbs")
MAX_MAINTENANCE_HOSTS = 30


def validate_maintenance_groups(
    groups: object,
    *,
    known_host_ids: Collection[str] | None = None,
) -> dict[str, list[str]]:
    """Normalize explicit selections; reject missing/duplicate/stale host IDs."""
    if not isinstance(groups, Mapping):
        raise ValueError("Automatic security update hosts are not configured")
    if set(groups) - set(MAINTENANCE_STAGES):
        raise ValueError("Invalid automatic security maintenance stage")

    seen: set[str] = set()
    result: dict[str, list[str]] = {}
    for stage in MAINTENANCE_STAGES:
        selected = groups.get(stage, [])
        if not isinstance(selected, list):
            raise ValueError("Invalid host selections; expected lists of PatchMon IDs")
        result[stage] = []
        for raw_id in selected:
            if not isinstance(raw_id, str) or not raw_id.strip() or raw_id != raw_id.strip():
                raise ValueError("Invalid host ID in automatic security maintenance")
            if raw_id in seen:
                raise ValueError(f"Duplicate security maintenance host: {raw_id}")
            if known_host_ids is not None and raw_id not in known_host_ids:
                raise ValueError(f"Security maintenance host missing from inventory: {raw_id}")
            seen.add(raw_id)
            result[stage].append(raw_id)
            if len(seen) > MAX_MAINTENANCE_HOSTS:
                raise ValueError("Too many automatic security maintenance hosts (maximum 30)")
    return result


def selected_maintenance_host_ids(
    groups: object,
    *,
    known_host_ids: Collection[str] | None = None,
) -> list[str]:
    """Return saved hosts in stage order or fail closed when no hosts opted in."""
    normalized = validate_maintenance_groups(groups, known_host_ids=known_host_ids)
    host_ids = [host_id for stage in MAINTENANCE_STAGES for host_id in normalized[stage]]
    if not host_ids:
        raise ValueError(
            "No automatic security maintenance hosts selected. "
            "Open PatchMon > Configure > Automatic security updates first."
        )
    return host_ids
