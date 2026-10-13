"""Runnable contract tests for the PatchMon v0.2 REST client (no HA install needed)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest

# This is a dependency-free contract test. Home Assistant bundles aiohttp in
# production, but a bare Python environment (e.g. Debian LXC) need not have it.
# Supply only the symbols used by the REST client when aiohttp is unavailable.
try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError as err:
    if err.name != "aiohttp":
        raise

    aiohttp = types.ModuleType("aiohttp")

    class BasicAuth:
        def __init__(self, login: str, password: str) -> None:
            self.login = login
            self.password = password

    class ClientError(Exception):
        pass

    aiohttp.BasicAuth = BasicAuth
    aiohttp.ClientError = ClientError
    aiohttp.ClientResponse = object
    aiohttp.ClientSession = object
    sys.modules["aiohttp"] = aiohttp


API_PATH = Path(__file__).parents[1] / "custom_components" / "patchmon" / "api.py"
SPEC = importlib.util.spec_from_file_location("patchmon_api_v02", API_PATH)
assert SPEC is not None and SPEC.loader is not None
api = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = api
SPEC.loader.exec_module(api)


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    async def json(self, content_type=None):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected HTTP call")
        return self.responses.pop(0)


class PatchMonClientV02Tests(unittest.IsolatedAsyncioTestCase):
    async def test_watched_packages_request_full_inventory(self):
        response = {"packages": [
            {"name": "openssl", "current_version": "3.0", "needs_update": False},
            {"name": "curl", "current_version": "7.9", "needs_update": True},
        ]}
        session = FakeSession(FakeResponse(200, response))
        client = api.PatchMonClient(session, "https://patchmon.example", "id", "secret")
        packages = await client.async_get_packages("host-1", updates_only=False)
        self.assertEqual(len(packages), 2)
        self.assertEqual(session.requests[0][2]["params"], {"updates_only": "false"})
        self.assertEqual(session.requests[0][2]["auth"].login, "id")

    async def test_patch_and_history_share_auth_boundary(self):
        session = FakeSession(
            FakeResponse(200, {"token": "jwt"}),
            FakeResponse(201, {"id": "run-1", "status": "queued"}),
            FakeResponse(200, {"runs": [{"id": "run-1", "status": "queued"}]}),
        )
        client = api.PatchMonClient(
            session, "https://patchmon.example/api/v1/", "id", "secret",
            username="ha", password="secret",
        )
        await client.async_patch_host("host-1", package_names=[" nginx "])
        runs = await client.async_get_patch_runs("host-1")
        self.assertEqual(runs[0]["status"], "queued")
        self.assertEqual(session.requests[1][2]["json"]["package_names"], ["nginx"])
        self.assertEqual(session.requests[1][2]["headers"]["Authorization"], "Bearer jwt")
        self.assertEqual(session.requests[2][2]["params"], {"host_id": "host-1", "limit": 1})
        self.assertEqual(session.requests[2][2]["headers"]["Authorization"], "Bearer jwt")

    async def test_expired_jwt_is_retried_once(self):
        session = FakeSession(
            FakeResponse(200, {"token": "old"}),
            FakeResponse(401, {"error": "expired"}),
            FakeResponse(200, {"token": "new"}),
            FakeResponse(201, {"id": "run-2"}),
        )
        client = api.PatchMonClient(session, "https://patchmon.example", "id", "key", username="ha", password="pw")
        self.assertEqual((await client.async_patch_host("host-1"))["id"], "run-2")
        self.assertEqual(session.requests[-1][2]["headers"]["Authorization"], "Bearer new")
        self.assertEqual(sum("/auth/login" in row[1] for row in session.requests), 2)

    async def test_missing_credentials_cannot_queue_patch(self):
        client = api.PatchMonClient(FakeSession(), "https://patchmon.example", "id", "key")
        with self.assertRaises(api.PatchMonAuthenticationError):
            await client.async_patch_host("host-1", package_names=["nginx"])

    async def test_api_permission_error(self):
        client = api.PatchMonClient(
            FakeSession(FakeResponse(403, {"error": "missing scope"})),
            "https://patchmon.example", "id", "key"
        )
        with self.assertRaises(api.PatchMonPermissionError):
            await client.async_get_packages("host-1")


if __name__ == "__main__":
    unittest.main()
