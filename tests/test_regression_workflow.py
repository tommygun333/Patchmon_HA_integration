"""Pure-Python regression coverage for PatchMon HA-only maintenance."""
import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "patchmon"
package = types.ModuleType("patchmon")
package.__path__ = [str(MODULE_ROOT)]
sys.modules.setdefault("patchmon", package)
security = importlib.import_module("patchmon.security_maintenance")
work = importlib.import_module("patchmon.maintenance_workflow")

NOW = datetime.now(timezone.utc)
HOST = {"id": "host-a", "hostname": "host-a", "effective_status": "active",
        "last_update": (NOW - timedelta(minutes=5)).isoformat(),
        "security_updates_count": 1}
PACKAGES = [
    {"name": "openssl", "needs_update": True, "is_security_update": True,
     "current_version": "1.0", "available_version": "1.1"},
    {"name": "nginx", "needs_update": True, "is_security_update": False,
     "current_version": "1.0", "available_version": "1.2"},
]


class Dummy:
    def __init__(self, *, fail_validation=False, remove=False, hosts=None, packages=None):
        self.hosts = hosts if hosts is not None else [dict(HOST)]
        self.packages = packages if packages is not None else PACKAGES.copy()
        self.calls = []
        self.approvals = []
        self.install_started = False
        self.fail_validation = fail_validation
        self.remove = remove

    async def async_get_hosts(self, groups=None):
        result = [dict(h) for h in self.hosts]
        if self.install_started:
            for h in result:
                h['last_update'] = (NOW + timedelta(minutes=1)).isoformat()
        return result

    async def async_get_updates(self, host_id):
        return [] if self.install_started else [dict(p) for p in self.packages]

    async def async_get_packages(self, host_id, updates_only=True):
        return [{"name": p["name"], "current_version": p.get("available_version")}
                for p in self.packages]

    async def async_patch_host(self, host_id, **kwargs):
        assert kwargs['dry_run'] is True, 'Validation must not install'
        assert kwargs['package_names'], 'Empty list must never become patch_all'
        self.calls.append((host_id, kwargs))
        return {'patch_run_id': 'validation-' + host_id}

    async def async_get_patch_run(self, run_id):
        if run_id.startswith('validation-'):
            names = self.calls[-1][1]['package_names']
            return {
                'id': run_id,
                'status': 'failed' if self.fail_validation else 'validated',
                'dry_run': True, 'patch_type': 'patch_package',
                'package_names': names,
                'packages_affected': names + ['libssl3'],
                'shell_output': 'Remv libimportant [1.0]' if self.remove else 'Inst openssl [1.0] (1.1)',
            }
        return {'id': run_id, 'status': 'completed', 'packages_affected': ['openssl', 'libssl3']}

    async def async_approve_patch_run(self, run_id, **kwargs):
        self.approvals.append((run_id, kwargs))
        self.install_started = True
        return {'patch_run_id': 'install-a', 'validation_run_id': run_id}


class SecurityTests(unittest.TestCase):
    def test_only_security_flagged_names_selected(self):
        plan = security.prepare_security_plan('host-a', HOST, PACKAGES)
        self.assertEqual(plan['state'], 'ready')
        self.assertEqual([x['name'] for x in plan['security_packages']], ['openssl'])
        self.assertEqual(plan['regular_count'], 1)

    def test_no_security_does_not_become_patch_all(self):
        plan = security.prepare_security_plan(
            'host-a', dict(HOST, security_updates_count=0), [PACKAGES[1]],
        )
        self.assertEqual(plan['state'], 'no_security_updates')
        self.assertEqual(plan['security_packages'], [])

    def test_unreported_security_flag_blocks(self):
        self.assertEqual(security.prepare_security_plan(
            'host-a', dict(HOST, security_updates_count=0), PACKAGES,
        )['state'], 'blocked')

    def test_detect_apt_removal(self):
        self.assertEqual(security.apt_removals('Inst nginx\nRemv libold [1]'), ['libold'])


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_install_approves_matching_validation_and_verifies(self):
        client = Dummy()
        report = await work.run_security_maintenance(client, ['host-a'], mode='install')
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['hosts'][0]['state'], 'verified')
        self.assertEqual(client.calls[0][1]['package_names'], ['openssl'])
        self.assertEqual(client.approvals, [
            ('validation-host-a', {'run_immediately': True}),
        ])
        self.assertEqual(report['hosts'][0]['validation']['extra_dependencies'], ['libssl3'])

    async def test_validate_does_not_approve(self):
        client = Dummy()
        report = await work.run_security_maintenance(client, ['host-a'], mode='validate')
        self.assertEqual(report['hosts'][0]['state'], 'validated')
        self.assertEqual(client.approvals, [])

    async def test_no_updates_does_not_call_patch(self):
        client = Dummy(hosts=[dict(HOST, security_updates_count=0)], packages=[PACKAGES[1]])
        report = await work.run_security_maintenance(client, ['host-a'], mode='install')
        self.assertEqual(report['hosts'][0]['state'], 'no_security_updates')
        self.assertEqual(client.calls, [])
        self.assertEqual(client.approvals, [])

    async def test_failed_validation_never_approves(self):
        client = Dummy(fail_validation=True)
        report = await work.run_security_maintenance(client, ['host-a'], mode='install')
        self.assertEqual(report['hosts'][0]['state'], 'validation_failed')
        self.assertEqual(client.approvals, [])

    async def test_apt_removals_abort_install(self):
        client = Dummy(remove=True)
        report = await work.run_security_maintenance(client, ['host-a'], mode='install')
        self.assertEqual(report['hosts'][0]['state'], 'validation_failed')
        self.assertEqual(client.approvals, [])

    async def test_stale_report_blocks(self):
        client = Dummy(hosts=[dict(HOST, last_update=(NOW-timedelta(hours=3)).isoformat())])
        report = await work.run_security_maintenance(client, ['host-a'], mode='install')
        self.assertEqual(report['hosts'][0]['state'], 'blocked')

    async def test_failed_host_skips_next(self):
        client = Dummy(fail_validation=True)
        report = await work.run_security_maintenance(client, ['host-a', 'host-b'], mode='install')
        self.assertEqual([h['state'] for h in report['hosts']], ['validation_failed', 'not_started'])

    async def test_manual_preflight_includes_regular_updates(self):
        client = Dummy()
        result = await work.validate_pending_updates(client, 'host-a')
        self.assertTrue(result['success'])
        self.assertEqual(client.calls[0][1]['package_names'], ['nginx', 'openssl'])
        self.assertFalse(result['full_patch_all_simulation'])
        self.assertEqual(client.approvals, [])

    async def test_manual_preflight_empty_skips(self):
        client = Dummy(packages=[])
        result = await work.validate_pending_updates(client, 'host-a')
        self.assertEqual(result['status'], 'no_updates')
        self.assertFalse(client.calls)

    async def test_manual_preflight_refuses_over_100(self):
        client = Dummy(packages=[dict(PACKAGES[1], name=f'pkg-{i}') for i in range(101)])
        result = await work.validate_pending_updates(client, 'host-a')
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(client.calls)

    async def test_approval_rejects_mismatched_package_names(self):
        client = Dummy()
        await client.async_patch_host('host-a', package_names=['openssl'], dry_run=True)
        with self.assertRaisesRegex(ValueError, 'targets changed'):
            await work.approve_validated_run(
                client, 'validation-host-a', patch_type='patch_package', names=['nginx'],
            )
        self.assertFalse(client.approvals)

    async def test_different_run_id_rejected(self):
        class WrongRun(Dummy):
            async def async_get_patch_run(self, run_id):
                return dict(await super().async_get_patch_run(run_id), id='other')
        client = WrongRun()
        await client.async_patch_host('host-a', package_names=['openssl'], dry_run=True)
        with self.assertRaisesRegex(ValueError, 'unrelated run'):
            await work.wait_for_status(client, 'validation-host-a', frozenset({'validated'}))


if __name__ == '__main__':
    unittest.main()
