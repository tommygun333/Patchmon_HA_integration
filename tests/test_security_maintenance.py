"""Security-maintenance guardrail tests, with no Home Assistant dependency."""

import asyncio
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "custom_components/patchmon/security_maintenance.py"
spec = importlib.util.spec_from_file_location("patchmon_security_maintenance", MODULE)
security = importlib.util.module_from_spec(spec)
spec.loader.exec_module(security)

NOW = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
HOST = {
    "hostname": "lxc-example",
    "effective_status": "active",
    "last_update": (NOW - timedelta(minutes=5)).isoformat(),
    "security_updates_count": 1,
    "needs_reboot": False,
}
PACKAGES = [
    {"name": "openssl", "needs_update": True, "is_security_update": True,
     "current_version": "1.0", "available_version": "1.1"},
    {"name": "nginx", "needs_update": True, "is_security_update": False,
     "current_version": "1.0", "available_version": "1.2"},
]


class SecurityPlanTest(unittest.TestCase):
    def plan(self, host=None, packages=None):
        return security.prepare_security_plan(
            "host-1", HOST if host is None else host,
            PACKAGES if packages is None else packages, now=NOW,
        )

    def test_security_only_selection_preserves_regular_updates(self):
        plan = self.plan()
        self.assertEqual(plan["state"], "ready")
        self.assertEqual([p["name"] for p in plan["security_packages"]], ["openssl"])
        self.assertEqual(plan["regular_count"], 1)
        self.assertFalse(plan["execution_safe"])
        self.assertTrue(plan["validation_eligible"])

    def test_empty_update_set_does_not_patch_all(self):
        host = dict(HOST, security_updates_count=0)
        plan = self.plan(host=host, packages=[])
        self.assertEqual(plan["state"], "no_security_updates")
        self.assertEqual(plan["security_packages"], [])

    def test_regular_only_update_set_skipped(self):
        host = dict(HOST, security_updates_count=0)
        plan = self.plan(host=host)
        self.assertEqual(plan["state"], "blocked")  # flagged security item disagrees
        plan = self.plan(host=host, packages=[PACKAGES[1]])
        self.assertEqual(plan["state"], "no_security_updates")
        self.assertEqual(plan["regular_count"], 1)

    def test_stale_or_unknown_report_blocks(self):
        self.assertEqual(self.plan(host=dict(HOST, last_update="invalid"))["state"], "blocked")
        older = (NOW - timedelta(hours=3)).isoformat()
        self.assertEqual(self.plan(host=dict(HOST, last_update=older))["state"], "blocked")
        self.assertEqual(self.plan(host={})["state"], "blocked")

    def test_offline_host_blocks(self):
        self.assertEqual(
            self.plan(host=dict(HOST, effective_status="inactive"))["state"], "blocked"
        )

    def test_security_count_disagreement_blocks(self):
        self.assertEqual(
            self.plan(host=dict(HOST, security_updates_count=2))["state"], "blocked"
        )

    def test_unsafe_names_and_duplicate_names_block(self):
        pkgs = [dict(PACKAGES[0]), dict(PACKAGES[0])]
        self.assertEqual(self.plan(packages=pkgs)["state"], "blocked")
        pkgs = [dict(PACKAGES[0], name="openssl;curl")]
        self.assertEqual(self.plan(packages=pkgs)["state"], "blocked")

    def test_over_100_security_updates_blocks(self):
        packages = [dict(PACKAGES[0], name=f"pkg-{i}") for i in range(101)]
        host = dict(HOST, security_updates_count=101)
        self.assertEqual(self.plan(host=host, packages=packages)["state"], "blocked")


class DummyClient:
    def __init__(self, statuses, affected=None):
        self.statuses = iter(statuses)
        self.calls = []
        self.affected = affected

    async def async_patch_host(self, host_id, **kwargs):
        self.calls.append((host_id, kwargs))
        return {"patch_run_id": "r1"}

    async def async_get_patch_run(self, run_id):
        status = next(self.statuses)
        return {"status": status, "packages_affected": self.affected}


class SecurityValidationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plan = security.prepare_security_plan("host-1", HOST, PACKAGES, now=NOW)

    async def test_validation_is_dry_run_and_never_execution(self):
        client = DummyClient(["validated"], ["openssl", "libssl3", "nginx"])
        result = await security.validate_security_plan(client, self.plan)
        self.assertEqual(result["status"], "validated")
        self.assertTrue(result["success"])
        self.assertEqual(result["extra_dependencies"], ["libssl3", "nginx"])
        self.assertFalse(result["execution_safe"])
        self.assertEqual(client.calls, [("host-1", {
            "package_names": ["openssl"], "dry_run": True,
            "pending_approval": False, "run_immediately": True,
        })])

    async def test_blocked_or_empty_plan_never_calls_patch(self):
        client = DummyClient(["validated"])
        empty = security.prepare_security_plan(
            "host-1", dict(HOST, security_updates_count=0), [], now=NOW,
        )
        outcome = await security.validate_security_plan(client, empty)
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(client.calls, [])

    async def test_failed_status(self):
        client = DummyClient(["failed"])
        result = await security.validate_security_plan(client, self.plan)
        self.assertFalse(result["success"])

    async def test_invalid_empty_run_id(self):
        class BrokenClient(DummyClient):
            async def async_patch_host(self, host_id, **kwargs):
                return {}
        self.assertEqual(
            (await security.validate_security_plan(BrokenClient([]), self.plan))["status"],
            "failed",
        )


if __name__ == "__main__":
    unittest.main()
