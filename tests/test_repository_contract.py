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
        guide = (ROOT / 'docs' / 'security-maintenance.md').read_text()
        for value in ('mode: attention', 'mode: viewer', 'mode: host',
                      'patchmon.get_security_reports', 'require_backup_checks'):
            self.assertIn(value, guide)
        for name in ('dashboard_cards.yaml', 'weekly_security_automation.yaml',
                     'manual_actions.yaml'):
            self.assertTrue((ROOT / 'docs' / 'examples' / name).exists())


if __name__ == '__main__':
    unittest.main()
