"""Exercise post-patch run -> inventory refresh without Home Assistant installed."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).parents[1] / "custom_components" / "patchmon"


class FakeDataUpdateCoordinator:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, name, config_entry, update_interval, always_update):
        self.hass = hass
        self.data = None

    def async_set_updated_data(self, data):
        self.data = data


def load_coordinator_module():
    """Load coordinator with HA API stubs, restoring the module registry."""
    pkg = "patchmon_coordinator_test_pkg"
    package = types.ModuleType(pkg)
    package.__path__ = []
    api = types.ModuleType(f"{pkg}.api")
    class PatchMonError(Exception):
        pass
    class PatchMonAuthenticationError(PatchMonError):
        pass
    api.PatchMonError = PatchMonError
    api.PatchMonAuthenticationError = PatchMonAuthenticationError
    api.PatchMonClient = object
    const = types.ModuleType(f"{pkg}.const")
    for key, val in {
        "CONF_HOST_GROUPS": "host_groups", "CONF_INCLUDE_PACKAGES": "include_packages",
        "CONF_MONITOR_PATCH_RUNS": "monitor_patch_runs", "CONF_SCAN_INTERVAL": "scan_interval",
        "CONF_TRACKED_PACKAGES": "tracked_packages", "DEFAULT_INCLUDE_PACKAGES": True,
        "DEFAULT_MONITOR_PATCH_RUNS": False, "DEFAULT_SCAN_INTERVAL": 15,
        "DOMAIN": "patchmon",
    }.items():
        setattr(const, key, val)
    ha = types.ModuleType("homeassistant")
    entries = types.ModuleType("homeassistant.config_entries")
    entries.ConfigEntry = object
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})
    helpers = types.ModuleType("homeassistant.helpers")
    uc = types.ModuleType("homeassistant.helpers.update_coordinator")
    uc.DataUpdateCoordinator = FakeDataUpdateCoordinator
    uc.UpdateFailed = type("UpdateFailed", (Exception,), {})
    modules = {
        pkg: package, f"{pkg}.api": api, f"{pkg}.const": const,
        "homeassistant": ha, "homeassistant.config_entries": entries,
        "homeassistant.core": core, "homeassistant.exceptions": exceptions,
        "homeassistant.helpers": helpers, "homeassistant.helpers.update_coordinator": uc,
    }
    with patch.dict(sys.modules, modules):
        utils_spec = importlib.util.spec_from_file_location(f"{pkg}.patch_utils", ROOT / "patch_utils.py")
        assert utils_spec and utils_spec.loader
        utils = importlib.util.module_from_spec(utils_spec)
        sys.modules[utils_spec.name] = utils
        utils_spec.loader.exec_module(utils)
        try:
            spec = importlib.util.spec_from_file_location(f"{pkg}.coordinator", ROOT / "coordinator.py")
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(utils_spec.name, None)
            sys.modules.pop(f"{pkg}.coordinator", None)
    return module


class FollowPatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_run_waits_until_agent_reports_new_version(self):
        mod = load_coordinator_module()
        client = types.SimpleNamespace(
            patching_enabled=True,
            async_get_patch_run=AsyncMock(side_effect=[
                {"id": "run-1", "status": "running", "patch_type": "patch_package", "package_names": ["openssl"]},
                {"id": "run-1", "status": "completed", "patch_type": "patch_package", "package_names": ["openssl"], "shell_output": "Upgrade done"},
            ]),
            async_get_packages=AsyncMock(side_effect=[
                [{"name": "openssl", "current_version": "1", "needs_update": True}],
                [{"name": "openssl", "current_version": "2", "needs_update": False}],
            ]),
        )
        entry = types.SimpleNamespace(entry_id="entry-1", data={}, options={
            "tracked_packages": {"h-1": ["openssl"]}
        }, async_on_unload=lambda cb: None)
        coordinator = mod.PatchMonCoordinator(types.SimpleNamespace(), entry, client)
        coordinator.data = {"h-1": {"_packages_loaded": True,
            "_tracked_packages": [{"name": "openssl", "current_version": "1", "needs_update": True}]}}
        published = []
        publish = coordinator.publish_patch_run
        def track_publish(host_id, run):
            published.append((run.get("status"), run.get("_ha_phase")))
            publish(host_id, run)
        coordinator.publish_patch_run = track_publish
        with patch.object(mod.asyncio, "sleep", new_callable=AsyncMock):
            await coordinator._watch_patch_run("h-1", "openssl", "run-1")
        self.assertEqual(published, [
            ("running", None), ("completed", "verifying"), ("completed", "completed")
        ])
        self.assertEqual(client.async_get_patch_run.await_count, 2)
        self.assertEqual(client.async_get_packages.await_count, 2)
        self.assertEqual(coordinator.data["h-1"]["_tracked_packages"][0]["current_version"], "2")
        self.assertEqual(coordinator.data["h-1"]["_patch_run"]["status"], "completed")
        self.assertEqual(coordinator.data["h-1"]["_patch_run"]["_ha_phase"], "completed")

    async def test_failed_run_reports_error_without_claiming_success(self):
        mod = load_coordinator_module()
        client = types.SimpleNamespace(
            patching_enabled=True,
            async_get_patch_run=AsyncMock(return_value={
                "id": "run-2", "status": "failed", "patch_type": "patch_package",
                "package_names": ["openssl"], "error_message": "apt failed",
            }),
            async_get_packages=AsyncMock(return_value=[
                {"name": "openssl", "current_version": "1", "needs_update": True},
            ]),
        )
        entry = types.SimpleNamespace(entry_id="entry-1", data={}, options={
            "tracked_packages": {"h-1": ["openssl"]}
        }, async_on_unload=lambda cb: None)
        coordinator = mod.PatchMonCoordinator(types.SimpleNamespace(), entry, client)
        coordinator.data = {"h-1": {"_packages_loaded": True, "_tracked_packages": [
            {"name": "openssl", "current_version": "1", "needs_update": True}
        ]}}
        with patch.object(mod.asyncio, "sleep", new_callable=AsyncMock):
            await coordinator._watch_patch_run("h-1", "openssl", "run-2")
        run = coordinator.data["h-1"]["_patch_run"]
        self.assertEqual(run["_ha_phase"], "errored")
        self.assertEqual(run["error_message"], "apt failed")
        client.async_get_packages.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
