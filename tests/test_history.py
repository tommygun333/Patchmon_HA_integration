"""Persistent history, sorting, acknowledgment, and restart-recovery tests."""
import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "patchmon"
package = types.ModuleType("patchmon")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("patchmon", package)
history_module = importlib.import_module("patchmon.maintenance_history")
work = importlib.import_module("patchmon.maintenance_workflow")
History = history_module.MaintenanceHistory


class InMemoryStore:
    def __init__(self, state=None):
        self.value = state or {}
        self.saves = 0
    async def async_load(self):
        return self.value
    async def async_save(self, state):
        self.value = state
        self.saves += 1


class RecoveryClient:
    def __init__(self, statuses=None, recent=None):
        self.statuses = statuses or {}
        self.recent = recent or []
        self.approvals = 0
    async def async_get_patch_run(self, rid):
        return {"id": rid, "status": self.statuses[rid]}
    async def async_get_patch_runs(self, host_id, limit=1):
        return list(self.recent)
    async def async_approve_patch_run(self, rid):
        self.approvals += 1
        raise AssertionError("Recovery must NEVER approve an install")


async def verify(client, host_id, before, names):
    return {"status": "verified", "installed_versions_after": {n: "2" for n in names},
            "reboot_required": True}


class TestHistory(unittest.IsolatedAsyncioTestCase):
    async def test_limit_and_reverse_chronological_sort(self):
        store = InMemoryStore({"reports": [
            {"report_id": f"id{i}", "started_at": f"2026-11-{i:02d}T03:30:00+00:00"}
            for i in range(1, 13)]})
        h = History(store, limit=5)
        await h.load()
        self.assertEqual([x['report_id'] for x in h.list()], ['id12','id11','id10','id9','id8'])

    async def test_persists_unread_ack_and_host_history(self):
        store = InMemoryStore()
        h = History(store, limit=3)
        await h.load()
        for i in range(1, 5):
            await h.start({"report_id": str(i), "started_at": f"2026-11-0{i}T03:30:00+00:00",
                           "hosts": [{"host_id": "host-a", "hostname": "lab", "state": "verified"}]})
            await h.finish({"report_id": str(i), "started_at": f"2026-11-0{i}T03:30:00+00:00",
                            "hosts": [{"host_id": "host-a", "hostname": "lab", "state": "verified"}]})
        self.assertEqual([r["report_id"] for r in h.list()], ["4","3","2"])
        self.assertTrue(h.unread)
        self.assertEqual(h.latest_host('host-a')[0]['report_id'], '4')
        await h.acknowledge('4')
        self.assertFalse(h.unread)
        restored = History(store)
        await restored.load()
        self.assertFalse(restored.unread)
        self.assertEqual(restored.latest()["report_id"], "4")
        self.assertEqual(restored.list(host_id='unknown'), [])
        with self.assertRaises(ValueError):
            await restored.acknowledge('3')

    async def test_sources_have_independent_retention_and_automation_ack(self):
        store = InMemoryStore()
        history = History(store, limit=2)
        await history.load()
        for index, source in enumerate(
            ("automation", "manual", "automation", "manual", "automation", "manual"),
            start=1,
        ):
            await history.finish({
                "report_id": f"r{index}",
                "report_source": source,
                "started_at": f"2026-11-0{index}T03:30:00+00:00",
                "hosts": [{"host_id": "host-a", "state": "validated"}],
            })
        self.assertEqual(
            [report["report_id"] for report in history.list(limit=10)],
            ["r6", "r5"],
        )
        self.assertEqual(
            [report["report_id"] for report in history.list(
                limit=10, report_source="manual"
            )],
            ["r6", "r4"],
        )
        self.assertEqual(
            [report["report_id"] for report in history.list(
                limit=10, report_source="automation"
            )],
            ["r5", "r3"],
        )
        self.assertTrue(history.automation_unread)
        await history.acknowledge_automation("r5")
        self.assertFalse(history.automation_unread)
        self.assertTrue(history.unread)
        await history.acknowledge("r6")
        self.assertFalse(history.unread)

    async def test_legacy_reports_migrate_to_automation_source(self):
        store = InMemoryStore({
            "reports": [{"report_id": "legacy", "started_at": "2026-11-01T03:30:00+00:00"}],
            "acknowledged": "legacy",
        })
        history = History(store)
        await history.load()
        self.assertEqual(history.latest()["report_source"], "automation")
        self.assertFalse(history.automation_unread)
        self.assertEqual(store.value["automation_acknowledged"], "legacy")

    async def test_known_install_recovery_does_not_approve(self):
        store = InMemoryStore({"active": {
            "report_id": "x", "started_at": "2026-11-04T03:30:00+00:00",
            "requested_host_ids": ['host-a', 'host-b'],
            "hosts": [{"host_id": "host-a", "state": "installing",
                       "last_report": "2026-11-04T01:00:00+00:00",
                       "security_packages": [{"name": "openssl"}],
                       "installation": {"run_id": "install1", "validation_run_id": "validate1"}}]}})
        h = History(store)
        await h.load()
        c = RecoveryClient(statuses={'install1':'completed'})
        result = await h.reconcile(c, verify)
        self.assertEqual(c.approvals, 0)
        self.assertIsNone(h.active)
        self.assertEqual(result['hosts'][0]['state'], 'recovered_needs_attention')
        self.assertEqual(result['hosts'][1]['state'], 'not_started')
        self.assertEqual(result['summary']['reboot_required'], 1)
        self.assertTrue(h.unread)

    async def test_approval_boundary_links_existing_installation(self):
        store = InMemoryStore({"active": {"report_id":"r2", "started_at":"2026-11-04T03:30:00+00:00",
            "hosts": [{"host_id":"host-a", "state":"approving", "validation":{"run_id":"v-1"},
                       "security_packages":[{"name":"openssl"}], "last_report":"2026-11-04T02:00:00+00:00"}]}})
        h = History(store)
        await h.load()
        c = RecoveryClient(statuses={'install1':'running'}, recent=[
            {"id":"install1", "validation_run_id":"v-1"}])
        result = await h.reconcile(c, verify)
        self.assertEqual(result['status'], 'interrupted_running')
        self.assertEqual(h.active['hosts'][0]['installation']['run_id'], 'install1')
        self.assertEqual(c.approvals, 0)

    async def test_unresolved_approval_prevents_another_cycle(self):
        store = InMemoryStore({"active": {"report_id":"r3",
            "hosts":[{"host_id":"host-a", "state":"approving", "validation":{"run_id":"v-1"}}]}})
        h = History(store)
        await h.load()
        c = RecoveryClient(recent=[])
        result = await h.reconcile(c, verify)
        self.assertEqual(result['hosts'][0]['state'], 'recovery_needs_manual_review')
        self.assertIsNotNone(h.active)
        with self.assertRaises(ValueError):
            await h.start({"report_id":"r4"})

    async def test_progress_checkpoint_survives_restart(self):
        store = InMemoryStore()
        h = History(store)
        await h.load()
        await h.start({"report_id":"abc", "status":"running", "hosts":[]})
        await h.checkpoint({"report_id":"abc", "status":"running", "hosts":[{"host_id":"host-a", "state":"validating"}]})
        r = History(store)
        await r.load()
        self.assertEqual(r.active['hosts'][0]['state'], 'validating')
        self.assertGreaterEqual(store.saves, 2)


class TestVersionVerification(unittest.IsolatedAsyncioTestCase):
    async def test_missing_selected_package_not_verified(self):
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
        new = datetime.now(timezone.utc).isoformat()
        class Client:
            async def async_get_hosts(self):
                return [{"id":"h", "last_update":new, "effective_status":"active"}]
            async def async_get_updates(self, host_id):
                return []
            async def async_get_packages(self, host_id, updates_only=False):
                return []
        result = await work.verify_post_patch(Client(), 'h', old, ['openssl'], poll_seconds=0)
        self.assertEqual(result['status'], 'needs_attention')
        self.assertEqual(result['missing_selected_packages'], ['openssl'])

if __name__ == '__main__':
    unittest.main()
