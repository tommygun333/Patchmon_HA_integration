"""Focused contract tests for the dependency-free PatchMon REST client logic."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest


class ClientError(Exception):
    """aiohttp ClientError test double."""


class BasicAuth:
    """aiohttp BasicAuth test double."""

    def __init__(self, login: str, password: str) -> None:
        self.login = login
        self.password = password


aiohttp = types.ModuleType("aiohttp")
aiohttp.BasicAuth = BasicAuth
aiohttp.ClientError = ClientError
aiohttp.ClientResponse = object
aiohttp.ClientSession = object
sys.modules.setdefault("aiohttp", aiohttp)

API_PATH = Path(__file__).parents[1] / "custom_components" / "patchmon" / "api.py"
SPEC = importlib.util.spec_from_file_location("patchmon_api_for_test", API_PATH)
assert SPEC and SPEC.loader
api = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = api
SPEC.loader.exec_module(api)


class FakeResponse:
    """Minimal JSON response."""

    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload


class FakeSession:
    """Capture requests and return queued responses."""

    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, str, dict]] = []

    async def request(self, method: str, url: str, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


class PatchMonClientTests(unittest.IsolatedAsyncioTestCase):
    def test_normalizes_server_urls(self) -> None:
        self.assertEqual(
            api.PatchMonClient.normalize_url(" https://patchmon.test/api/v1/ "),
            "https://patchmon.test",
        )
        self.assertEqual(
            api.PatchMonClient.normalize_url("http://patchmon:3000"),
            "http://patchmon:3000",
        )

    async def test_hosts_use_scoped_basic_auth_and_stats(self) -> None:
        session = FakeSession(FakeResponse(200, {"hosts": [{"id": "host-1"}]}))
        client = api.PatchMonClient(session, "https://patchmon.test", "id", "secret")

        hosts = await client.async_get_hosts("Production")

        self.assertEqual(hosts, [{"id": "host-1"}])
        method, url, kwargs = session.requests[0]
        self.assertEqual((method, url), ("GET", "https://patchmon.test/api/v1/api/hosts"))
        self.assertEqual(kwargs["params"], {"include": "stats", "hostgroup": "Production"})
        self.assertEqual(kwargs["auth"].login, "id")

    async def test_patch_all_logs_in_then_uses_bearer_token(self) -> None:
        session = FakeSession(
            FakeResponse(200, {"token": "jwt-token"}),
            FakeResponse(201, {"id": "run-1", "status": "queued"}),
        )
        client = api.PatchMonClient(
            session,
            "https://patchmon.test",
            "id",
            "secret",
            username="ha",
            password="password",
        )

        result = await client.async_patch_host("host-1", run_immediately=True)

        self.assertEqual(result["id"], "run-1")
        _, _, login_kwargs = session.requests[0]
        self.assertEqual(login_kwargs["json"], {"username": "ha", "password": "password"})
        _, patch_url, patch_kwargs = session.requests[1]
        self.assertEqual(patch_url, "https://patchmon.test/api/v1/patching/trigger")
        self.assertEqual(patch_kwargs["headers"]["Authorization"], "Bearer jwt-token")
        self.assertEqual(
            patch_kwargs["json"],
            {
                "host_id": "host-1",
                "patch_type": "patch_all",
                "dry_run": False,
                "pending_approval": False,
                "schedule_override": "immediate",
            },
        )

    async def test_selected_packages_use_patch_package(self) -> None:
        session = FakeSession(
            FakeResponse(200, {"token": "jwt-token"}),
            FakeResponse(201, {"id": "run-2"}),
        )
        client = api.PatchMonClient(
            session,
            "https://patchmon.test",
            "id",
            "secret",
            username="ha",
            password="password",
        )

        await client.async_patch_host(
            "host-1", package_names=[" openssl ", "nginx"], dry_run=True
        )

        body = session.requests[1][2]["json"]
        self.assertEqual(body["patch_type"], "patch_package")
        self.assertEqual(body["package_names"], ["openssl", "nginx"])
        self.assertTrue(body["dry_run"])

    async def test_authentication_error_is_translated(self) -> None:
        session = FakeSession(FakeResponse(401, {"error": "bad credentials"}))
        client = api.PatchMonClient(session, "https://patchmon.test", "id", "wrong")

        with self.assertRaisesRegex(api.PatchMonAuthenticationError, "bad credentials"):
            await client.async_get_hosts()

    async def test_dry_run_requires_selected_packages(self) -> None:
        client = api.PatchMonClient(
            FakeSession(),
            "https://patchmon.test",
            "id",
            "secret",
            username="ha",
            password="password",
        )

        with self.assertRaisesRegex(api.PatchMonError, "package_names"):
            await client.async_patch_host("host-1", dry_run=True)


if __name__ == "__main__":
    unittest.main()
