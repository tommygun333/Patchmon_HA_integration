"""Regression coverage for transient PatchMon HTTP reads; no HA install needed."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, call, patch

# The existing tests supply aiohttp stand-ins on CI without HA installed.
try:
    import aiohttp
except ModuleNotFoundError as err:
    if err.name != "aiohttp":
        raise
    aiohttp = types.ModuleType("aiohttp")
    class ClientError(Exception):
        pass
    class BasicAuth:
        def __init__(self, login, password):
            self.login, self.password = login, password
    class WSServerHandshakeError(ClientError):
        status = 401
    aiohttp.BasicAuth = BasicAuth
    aiohttp.ClientError = ClientError
    aiohttp.ClientResponse = object
    aiohttp.ClientSession = object
    aiohttp.WSServerHandshakeError = WSServerHandshakeError
    aiohttp.WSMsgType = types.SimpleNamespace(TEXT="TEXT", ERROR="ERROR")
    sys.modules["aiohttp"] = aiohttp

FILE = Path(__file__).resolve().parents[1] / "custom_components" / "patchmon" / "api.py"
SPEC = importlib.util.spec_from_file_location("patchmon_api_retry_test", FILE)
assert SPEC is not None and SPEC.loader is not None
api = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = api
SPEC.loader.exec_module(api)


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload
        self.released = 0
    async def json(self, content_type=None):
        return self.payload
    def release(self):
        self.released += 1


class FakeSession:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []
    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if not self.outcomes:
            raise AssertionError("Unexpected extra HTTP request")
        item = self.outcomes.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class RetryTests(unittest.IsolatedAsyncioTestCase):
    def new_client(self, session, *, patch=False):
        return api.PatchMonClient(session, "https://patchmon.example", "id", "key",
                                  **({"username": "ha", "password": "pass"} if patch else {}))

    async def test_get_retries_dns_errors_and_preserves_parameters(self):
        session = FakeSession(api.ClientError("DNS timeout"), api.ClientError("DNS unavailable"),
                              FakeResponse(200, {"hosts": [{"id": "h"}]}))
        with patch.object(api.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            result = await self.new_client(session).async_get_hosts("group")
        self.assertEqual([h["id"] for h in result], ["h"])
        self.assertEqual(len(session.requests), 3)
        self.assertEqual(sleep.await_args_list, [call(1.0), call(2.0)])
        self.assertEqual([r[2]["params"] for r in session.requests],
                         [{"include": "stats", "hostgroup": "group"}] * 3)

    async def test_get_retries_timeout_once(self):
        session = FakeSession(TimeoutError("resolver exceeded timeout"),
                              FakeResponse(200, {"packages": []}))
        with patch.object(api.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(await self.new_client(session).async_get_updates("host"), [])
        self.assertEqual(len(session.requests), 2)
        sleep.assert_awaited_once_with(1.0)

    async def test_get_retries_temporary_http_error_and_releases_response(self):
        first = FakeResponse(503, {"error": "unavailable"})
        session = FakeSession(first, FakeResponse(200, {"hosts": [{"id": "h"}]}))
        with patch.object(api.asyncio, "sleep", new_callable=AsyncMock):
            await self.new_client(session).async_get_hosts()
        self.assertEqual(len(session.requests), 2)
        self.assertEqual(first.released, 1)

    async def test_get_fails_after_three_transport_attempts(self):
        session = FakeSession(*(api.ClientError("DNS failure") for _ in range(3)))
        with patch.object(api.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaisesRegex(api.PatchMonConnectionError, "after 3 attempt"):
                await self.new_client(session).async_get_hosts()
        self.assertEqual(len(session.requests), 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_unauthorized_and_forbidden_do_not_retry(self):
        for status, exc in ((401, api.PatchMonAuthenticationError),
                            (403, api.PatchMonPermissionError)):
            with self.subTest(status=status):
                session = FakeSession(FakeResponse(status, {"error": "denied"}))
                with patch.object(api.asyncio, "sleep", new_callable=AsyncMock) as sleep:
                    with self.assertRaises(exc):
                        await self.new_client(session).async_get_hosts()
                self.assertEqual(len(session.requests), 1)
                sleep.assert_not_awaited()

    async def test_rate_limit_is_not_retried(self):
        session = FakeSession(FakeResponse(429, {"error": "too many requests"}))
        with patch.object(api.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(api.PatchMonError):
                await self.new_client(session).async_get_hosts()
        self.assertEqual(len(session.requests), 1)
        sleep.assert_not_awaited()

    async def test_invalid_success_json_is_not_retried(self):
        session = FakeSession(FakeResponse(200, []))
        with patch.object(api.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(api.PatchMonError):
                await self.new_client(session).async_get_hosts()
        self.assertEqual(len(session.requests), 1)
        sleep.assert_not_awaited()

    async def test_patch_trigger_post_is_never_replayed(self):
        session = FakeSession(FakeResponse(200, {"token": "jwt"}),
                              TimeoutError("Response lost after submission"))
        with patch.object(api.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(api.PatchMonConnectionError):
                await self.new_client(session, patch=True).async_patch_host("host")
        self.assertEqual([x[0] for x in session.requests], ["POST", "POST"])
        self.assertTrue(session.requests[-1][1].endswith("/patching/trigger"))
        sleep.assert_not_awaited()

    async def test_approval_post_is_never_replayed(self):
        session = FakeSession(FakeResponse(200, {"token": "jwt"}),
                              api.ClientError("Socket disconnected"))
        with patch.object(api.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(api.PatchMonConnectionError):
                await self.new_client(session, patch=True).async_approve_patch_run("v-1")
        self.assertEqual(len(session.requests), 2)
        sleep.assert_not_awaited()

    async def test_login_can_retry_without_replaying_patch_writes(self):
        session = FakeSession(api.ClientError("DNS temporarily unreachable"),
                              FakeResponse(200, {"token": "jwt"}),
                              FakeResponse(200, {"runs": []}))
        with patch.object(api.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(await self.new_client(session, patch=True).async_get_patch_runs("host"), [])
        self.assertEqual([x[0] for x in session.requests], ["POST", "POST", "GET"])
        sleep.assert_awaited_once_with(1.0)

    async def test_cancelled_request_is_never_retried(self):
        session = FakeSession(asyncio.CancelledError())
        with patch.object(api.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await self.new_client(session).async_get_hosts()
        self.assertEqual(len(session.requests), 1)
        sleep.assert_not_awaited()

    async def test_http_503_exhaustion_reports_original_error(self):
        session = FakeSession(*(FakeResponse(503, {"error": "server busy"}) for _ in range(3)))
        with patch.object(api.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaisesRegex(api.PatchMonError, "server busy"):
                await self.new_client(session).async_get_hosts()
        self.assertEqual(len(session.requests), 3)
        self.assertEqual(sleep.await_count, 2)


if __name__ == "__main__":
    unittest.main()
