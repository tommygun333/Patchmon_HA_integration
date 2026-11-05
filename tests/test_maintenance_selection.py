"""Contract tests for host opt-in and deterministic maintenance sequencing.

No Home Assistant installation is required to run these tests.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / 'custom_components' / 'patchmon' / 'maintenance_selection.py'
SPEC = importlib.util.spec_from_file_location('patchmon_maintenance_selection_test', PATH)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class SecurityMaintenanceSelectionTests(unittest.TestCase):
    def test_selection_preserves_stage_order(self):
        config = {
            'pbs': ['pbs-1'], 'critical': ['dns-1'],
            'standard': ['lab-1', 'media-1'], 'proxmox': ['pve-1'],
        }
        self.assertEqual(mod.selected_maintenance_host_ids(config),
                         ['pve-1', 'lab-1', 'media-1', 'dns-1', 'pbs-1'])

    def test_no_config_refuses_install(self):
        with self.assertRaisesRegex(ValueError, 'not configured'):
            mod.selected_maintenance_host_ids(None)

    def test_empty_selection_refuses_install(self):
        with self.assertRaisesRegex(ValueError, 'No automatic security'):
            mod.selected_maintenance_host_ids({})

    def test_duplicate_between_stages_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            mod.validate_maintenance_groups({'proxmox':['same'], 'pbs':['same']})

    def test_inventory_missing_host_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'missing from inventory'):
            mod.selected_maintenance_host_ids({'standard':['vanished']}, known_host_ids={'lab'})

    def test_new_host_not_auto_included(self):
        self.assertEqual(mod.selected_maintenance_host_ids({'standard':['lab']},
                         known_host_ids={'lab', 'discovered'}), ['lab'])

    def test_more_than_thirty_rejected(self):
        with self.assertRaisesRegex(ValueError, 'maximum 30'):
            mod.validate_maintenance_groups({'standard':[str(i) for i in range(31)]})

    def test_reject_bad_groups(self):
        for value in [{'standard': 'host'}, {'random': ['host']}, {'pbs': [None]},
                      {'proxmox': [' bad ']}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                mod.validate_maintenance_groups(value)

    def test_groups_can_be_cleared(self):
        self.assertEqual(mod.validate_maintenance_groups({}),
                         {stage: [] for stage in mod.MAINTENANCE_STAGES})


if __name__ == '__main__':
    unittest.main()
