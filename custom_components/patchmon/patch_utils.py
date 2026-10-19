"""Small, dependency-free PatchMon run helpers shared by entities and tests."""

from __future__ import annotations

from typing import Any

ACTIVE_RUN_STATUSES = frozenset({
    "queued", "scheduled", "running", "pending_validation", "pending_approval",
    "validating", "waiting", "delayed",
})
SUCCESS_RUN_STATUSES = frozenset({"completed", "success", "succeeded"})


def run_matches_package(run: dict[str, Any] | None, package: str) -> bool:
    """Only show package progress for packages selected for this run.

    A patch-all run does not necessarily update every installed package. Never
    claim that an already-current package is being installed. For a run without
    a pre-patch inventory, prefer no per-package progress to false progress.
    The host-level patch status and console remain available independently.
    """
    if not isinstance(run, dict):
        return False
    if run.get("patch_type") == "patch_all":
        targets = run.get("_ha_target_packages")
        return isinstance(targets, (list, tuple, set)) and package in targets
    packages = run.get("package_names")
    return run.get("package_name") == package or (
        isinstance(packages, list) and package in packages
    )


def post_patch_report_is_fresh(previous: Any, current: Any, completed: Any) -> bool:
    """True only when PatchMon reports an inventory newer than the patch run.

    The host's last_update belongs to the agent's inventory report, whereas a
    successful patch-run status only says that the command finished. Never
    equate the two; that would leave HA showing outdated package versions.
    """
    from datetime import datetime, timezone

    def parse(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)

    after = parse(current)
    before = parse(previous)
    finished = parse(completed)
    if after is None or (before is None and finished is None):
        return False
    if before is not None and after <= before:
        return False
    if finished is not None and after < finished:
        return False
    return True


def is_active_run(run: dict[str, Any] | None) -> bool:
    return isinstance(run, dict) and run.get("status") in ACTIVE_RUN_STATUSES


def console_tail(output: Any, *, max_chars: int = 2048, max_lines: int = 25) -> str:
    """Bound console output stored in recorder; full logs remain in PatchMon."""
    if not isinstance(output, str) or not output:
        return ""
    lines = output.splitlines()[-max_lines:]
    return "\n".join(lines)[-max_chars:]


# PatchMon exposes lifecycle states, not a measured installation percentage.
# These numbers are visual stage milestones only; never interpret them as
# fraction of packages or bytes installed.
RUN_STAGE_PROGRESS: dict[str, tuple[str, int]] = {
    "pending_approval": ("awaiting_approval", 5),
    "scheduled": ("scheduled", 5),
    "delayed": ("scheduled", 5),
    "queued": ("queued", 10),
    "waiting": ("queued", 10),
    "pending_validation": ("validating", 25),
    "validating": ("validating", 25),
    "running": ("updating", 55),
}


def run_progress(run: dict[str, Any] | None, package: str) -> tuple[bool, int | None, str | None]:
    """HA update progress (in_progress, stage milestone, stage label).

    Prefer HA's post-install inventory verification stage over PatchMon's
    terminal 'completed': an agent can report package versions later.
    """
    if not run_matches_package(run, package):
        return False, None, None
    phase = run.get("_ha_phase")
    if phase == "verifying":
        return True, 90, "verifying"
    if phase == "awaiting_inventory":
        return False, None, "awaiting_inventory"
    if phase == "completed":
        return False, None, "completed"
    if phase == "errored":
        return False, None, "errored"
    status = str(run.get("status") or "").lower()
    if status in RUN_STAGE_PROGRESS:
        stage, percentage = RUN_STAGE_PROGRESS[status]
        return True, percentage, stage
    if status in ACTIVE_RUN_STATUSES:
        return True, None, status
    if status in SUCCESS_RUN_STATUSES:
        return False, None, "completed"
    if status in {"failed", "error", "errored", "cancelled", "canceled", "stopped",
                  "timed_out", "disconnected", "aborted", "validation_failed"}:
        return False, None, "errored"
    return False, None, status or None
