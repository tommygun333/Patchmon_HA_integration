"""Isolated entity-contract tests, runnable without Home Assistant installed."""

from __future__ import annotations

import asyncio
from enum import IntFlag
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock


class Feature(IntFlag):
    INSTALL = 1


class FakeHostEntity:
    def __init__(self, coordinator, host_id):
        self.coordinator = coordinator
        self.host_id = host_id

    @property
    def host(self):
        return self.coordinator.host(self.host_id) or {}

    @property
    def available(self):
        return bool(self.coordinator.host(self.host_id))


class FakeClient:
    def __init__(self, auth=True):
        self.patching_enabled = auth
        self.async_patch_host = AsyncMock(return_value={"id": "test-run-id"})


class FakeCoordinator:
    def __init__(self, pkg, loaded=True, patch_auth=True):
        self.client = FakeClient(patch_auth)
        self.entry = types.SimpleNamespace(entry_id="entry-1")
        self._host = {"_packages_loaded": loaded, "_tracked_packages": [pkg]}
        self.async_request_refresh = AsyncMock()

    def host(self, host_id):
        return self._host if host_id == "h-1" else None

    def tracked_package(self, host_id, name):
        if host_id != "h-1":
            return None
        return next((p for p in self._host["_tracked_packages"] if p["name"] == name), None)


def _fake_module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod


# Only stub types the update module imports. The production files are unchanged.
_fake_module("homeassistant")
_fake_module("homeassistant.components")
_fake_module("homeassistant.components.update", UpdateEntity=type("UpdateEntity", (), {}), UpdateEntityFeature=Feature)
_fake_module("homeassistant.config_entries", ConfigEntry=object)
_fake_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_fake_module("homeassistant.exceptions", HomeAssistantError=RuntimeError)
_fake_module("homeassistant.helpers")
_fake_module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)

package_name = "patchmon_entity_test_pkg"
pkg = types.ModuleType(package_name)
pkg.__path__ = []
sys.modules[package_name] = pkg
_fake_module(f"{package_name}.api", PatchMonError=type("PatchMonError", (Exception,), {}))
_fake_module(f"{package_name}.coordinator", PatchMonCoordinator=FakeCoordinator)
_fake_module(f"{package_name}.entity", PatchMonHostEntity=FakeHostEntity)

update_path = Path(__file__).parents[1] / "custom_components" / "patchmon" / "update.py"
spec = importlib.util.spec_from_file_location(f"{package_name}.update", update_path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class PatchMonUpdateEntityTests(unittest.IsolatedAsyncioTestCase):
    def test_package_manager_versions_use_needs_update(self):
        pkg = {
            "name": "openssl", "current_version": "1:3.0.13-0ubuntu3.4",
            "available_version": "1:3.0.13-0ubuntu3.5", "needs_update": True,
            "is_security_update": True,
        }
        coordinator = FakeCoordinator(pkg)
        entity = module.PatchMonPackageUpdate(coordinator, "h-1", "openssl")
        self.assertTrue(entity.available)
        self.assertTrue(entity.version_is_newer(entity.latest_version, entity.installed_version))
        self.assertEqual(entity.extra_state_attributes["security_update"], True)
        self.assertEqual(entity.installed_version, pkg["current_version"])
        self.assertTrue(entity._attr_supported_features & Feature.INSTALL)
        pkg["needs_update"] = False
        self.assertEqual(entity.installed_version, entity.latest_version)
        self.assertFalse(entity.version_is_newer(entity.latest_version, entity.installed_version))

    async def test_install_sends_exact_package_not_patch_all(self):
        pkg = {"name": "openssl", "current_version": "1.0", "available_version": "1.1", "needs_update": True}
        coordinator = FakeCoordinator(pkg)
        entity = module.PatchMonPackageUpdate(coordinator, "h-1", "openssl")
        await entity.async_install(version=None, backup=False)
        coordinator.client.async_patch_host.assert_awaited_once_with("h-1", package_names=["openssl"])
        coordinator.async_request_refresh.assert_awaited_once()

    async def test_stale_inventory_refuses_install(self):
        pkg = {"name": "openssl", "current_version": "1.0", "available_version": "1.1", "needs_update": True}
        coordinator = FakeCoordinator(pkg, loaded=False)
        entity = module.PatchMonPackageUpdate(coordinator, "h-1", "openssl")
        self.assertFalse(entity.available)
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            await entity.async_install(version=None, backup=False)
        coordinator.client.async_patch_host.assert_not_awaited()

    async def test_matching_active_run_blocks_double_submission(self):
        pkg = {"name": "openssl", "current_version": "1.0", "available_version": "1.1", "needs_update": True}
        coordinator = FakeCoordinator(pkg)
        coordinator._host.update({
            "_patch_run_loaded": True,
            "_patch_run": {
                "status": "running", "patch_type": "patch_package",
                "package_names": ["openssl"],
            },
        })
        entity = module.PatchMonPackageUpdate(coordinator, "h-1", "openssl")
        self.assertTrue(entity.in_progress)
        with self.assertRaisesRegex(RuntimeError, "already in progress"):
            await entity.async_install(version=None, backup=False)
        coordinator.client.async_patch_host.assert_not_awaited()

    async def test_no_credentials_is_read_only(self):
        pkg = {"name": "openssl", "current_version": "1.0", "available_version": "1.1", "needs_update": True}
        coordinator = FakeCoordinator(pkg, patch_auth=False)
        entity = module.PatchMonPackageUpdate(coordinator, "h-1", "openssl")
        self.assertEqual(entity._attr_supported_features, Feature(0))
        with self.assertRaises(RuntimeError):
            await entity.async_install(version=None, backup=False)


if __name__ == "__main__":
    unittest.main()
