"""Repository hygiene and Home Assistant validation guards (no HA installed)."""
from __future__ import annotations

import json
import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMP = ROOT / 'custom_components' / 'patchmon'


class RepositoryContractTests(unittest.TestCase):
    def test_hassfest_manifest_order(self):
        parsed = json.loads((COMP / 'manifest.json').read_text())
        self.assertEqual(list(parsed), ['domain', 'name'] + sorted(
            k for k in parsed if k not in ('domain', 'name')))
        self.assertIn('http', parsed['dependencies'])

    def test_config_entry_only_schema(self):
        source = (COMP / '__init__.py').read_text()
        self.assertIn('CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)', source)

    def test_committed_backup_or_bytecode_is_not_shipped(self):
        completed = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'],
                                   stdout=subprocess.PIPE, check=True)
        paths = completed.stdout.decode().strip('\0').split('\0')
        offenders = [p for p in paths if p.startswith('custom_components/patchmon/') and
                     (p.endswith(('.bak', '.pyc', '.pyo')) or '/__pycache__/' in p)]
        self.assertEqual(offenders, [], f'Unwanted committed files: {offenders}')

    def test_documented_dashboard_resource_is_shipped(self):
        self.assertTrue((COMP / 'www' / 'patchmon-security-report-card.js').is_file())

    def test_report_panel_and_native_dialog_contract(self):
        manifest = json.loads((COMP / 'manifest.json').read_text())
        source = (COMP / 'www' / 'patchmon-security-report-card.js').read_text()
        self.assertIn('panel_custom', manifest['dependencies'])
        self.assertIn('patchmon-security-report-panel', source)
        self.assertIn('patchmon-security-report-dialog', source)
        self.assertIn('show-dialog', source)
        self.assertIn('report_source: this._cfg.report_source', source)
        self.assertIn('report_source: this._params.report_source', source)
        self.assertNotIn('if (!reportId) return;', source)
        self.assertNotIn('hide_when_read &&', source)
        guide = (ROOT / 'docs' / 'security-maintenance.md').read_text()
        for value in ('mode: attention', 'mode: viewer', 'mode: host',
                      'patchmon.get_security_reports', 'require_backup_checks'):
            self.assertIn(value, guide)
        for name in ('dashboard_cards.yaml', 'weekly_security_automation.yaml',
                     'manual_actions.yaml'):
            self.assertTrue((ROOT / 'docs' / 'examples' / name).exists())

    def test_inventory_timestamp_has_specific_name(self):
        source = (COMP / 'sensor.py').read_text()
        self.assertIn('_attr_name = "Last agent inventory"', source)
        self.assertNotIn('_attr_name = "Last report"', source)

    def test_report_unread_entities_are_automatic_informational_indicators(self):
        source = (COMP / 'maintenance_entities.py').read_text()
        unread_entities = source.split(
            'class PatchMonSecurityReportUnread', 1
        )[1].split('class PatchMonHostSecurityCycleSensor', 1)[0]
        self.assertNotIn('_attr_device_class', unread_entities)
        self.assertEqual(
            unread_entities.count('maintenance_reports.automation_unread'), 2
        )
        self.assertNotIn('maintenance_reports.unread', unread_entities)


if __name__ == '__main__':
    unittest.main()
