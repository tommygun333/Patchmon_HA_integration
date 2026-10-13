"""Small, dependency-free PatchMon run helpers shared by entities and tests."""

from __future__ import annotations

from typing import Any

ACTIVE_RUN_STATUSES = frozenset({
    "queued", "scheduled", "running", "pending_validation", "pending_approval",
    "validating", "waiting", "delayed",
})
SUCCESS_RUN_STATUSES = frozenset({"completed", "success", "succeeded"})


def run_matches_package(run: dict[str, Any] | None, package: str) -> bool:
    """Match both patch-all and specifically selected package runs."""
    if not isinstance(run, dict):
        return False
    if run.get("patch_type") == "patch_all":
        return True
    packages = run.get("package_names")
    return run.get("package_name") == package or (
        isinstance(packages, list) and package in packages
    )


def is_active_run(run: dict[str, Any] | None) -> bool:
    return isinstance(run, dict) and run.get("status") in ACTIVE_RUN_STATUSES


def console_tail(output: Any, *, max_chars: int = 2048, max_lines: int = 25) -> str:
    """Bound console output stored in recorder; full logs remain in PatchMon."""
    if not isinstance(output, str) or not output:
        return ""
    lines = output.splitlines()[-max_lines:]
    return "\n".join(lines)[-max_chars:]
