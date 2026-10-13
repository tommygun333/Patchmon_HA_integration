"""Check run-state, package matching and safe output limits without HA installed."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).parents[1] / "custom_components" / "patchmon" / "patch_utils.py"
SPEC = importlib.util.spec_from_file_location("patchmon_patch_utils_test", PATH)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class PatchRunUtilitiesTests(unittest.TestCase):
    def test_queued_and_running_are_active(self):
        for status in ("queued", "running", "scheduled", "pending_approval"):
            self.assertTrue(module.is_active_run({"status": status}))
        for status in ("completed", "failed", "cancelled"):
            self.assertFalse(module.is_active_run({"status": status}))

    def test_package_matching_ignores_other_package(self):
        run = {"patch_type": "patch_package", "package_names": ["openssl", "curl"]}
        self.assertTrue(module.run_matches_package(run, "curl"))
        self.assertFalse(module.run_matches_package(run, "nginx"))
        self.assertTrue(module.run_matches_package({"patch_type": "patch_all"}, "nginx"))

    def test_stage_progress_not_leaked_between_packages(self):
        run = {"status": "queued", "patch_type": "patch_package", "package_names": ["openssl"]}
        self.assertEqual(module.run_progress(run, "openssl"), (True, 10, "queued"))
        self.assertEqual(module.run_progress(run, "curl"), (False, None, None))
        run["status"] = "running"
        self.assertEqual(module.run_progress(run, "openssl"), (True, 55, "updating"))
        run["status"] = "completed"
        run["_ha_phase"] = "verifying"
        self.assertEqual(module.run_progress(run, "openssl"), (True, 90, "verifying"))
        run["_ha_phase"] = "completed"
        self.assertEqual(module.run_progress(run, "openssl"), (False, None, "completed"))

    def test_output_tail_is_bounded(self):
        output = "\n".join(f"line {n}" for n in range(100))
        tail = module.console_tail(output, max_chars=50, max_lines=3)
        self.assertTrue(tail.endswith("line 99"))
        self.assertNotIn("line 1\n", tail)
        self.assertLessEqual(len(tail), 50)
        self.assertEqual(module.console_tail(None), "")

    def test_local_translations_present(self):
        import json
        root = PATH.parent
        strings = json.loads((root / "strings.json").read_text())
        translations = json.loads((root / "translations" / "en.json").read_text())
        self.assertEqual(strings, translations)
        self.assertEqual(translations["options"]["step"]["init"]["menu_options"]["general"], "General settings")


if __name__ == "__main__":
    unittest.main()
