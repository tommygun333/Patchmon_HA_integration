"""Async REST client for PatchMon."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from typing import Any

from aiohttp import BasicAuth, ClientError, ClientResponse, ClientSession


class PatchMonError(Exception):
    """Base PatchMon client error."""


class PatchMonConnectionError(PatchMonError):
    """The PatchMon server could not be reached."""


class PatchMonAuthenticationError(PatchMonError):
    """PatchMon rejected the supplied credentials."""


class PatchMonPermissionError(PatchMonError):
    """The credentials do not have the required permission."""


class PatchMonTwoFactorRequired(PatchMonAuthenticationError):
    """The account requires interactive two-factor authentication."""


class PatchMonClient:
    """Client for PatchMon's scoped and user-authenticated APIs."""

    def __init__(
        self,
        session: ClientSession,
        base_url: str,
        api_id: str,
        api_key: str,
        *,
        verify_ssl: bool = True,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._session = session
        self.base_url = self.normalize_url(base_url)
        self._basic_auth = BasicAuth(api_id, api_key)
        self._verify_ssl = verify_ssl
        self._username = username or None
        self._password = password or None
        self._access_token: str | None = None

    @staticmethod
    def normalize_url(url: str) -> str:
        """Return the PatchMon origin without a trailing API path."""
        normalized = url.strip().rstrip("/")
        for suffix in ("/api/v1", "/api"):
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)]
                break
        return normalized.rstrip("/")

    @property
    def patching_enabled(self) -> bool:
        """Return whether user credentials were configured."""
        return bool(self._username and self._password)

    async def async_get_hosts(self, host_groups: str | None = None) -> list[dict[str, Any]]:
        """Return all visible hosts with inline statistics."""
        params: dict[str, str] = {"include": "stats"}
        if host_groups:
            params["hostgroup"] = host_groups
        payload = await self._request(
            "GET", "/api/v1/api/hosts", auth="basic", params=params
        )
        hosts = payload.get("hosts") if isinstance(payload, dict) else None
        if not isinstance(hosts, list):
            raise PatchMonError("PatchMon returned an invalid hosts response")
        return [host for host in hosts if isinstance(host, dict) and host.get("id")]

    async def async_get_updates(self, host_id: str) -> list[dict[str, Any]]:
        """Return packages with an available update for a host."""
        payload = await self._request(
            "GET",
            f"/api/v1/api/hosts/{host_id}/packages",
            auth="basic",
            params={"updates_only": "true"},
        )
        packages = payload.get("packages") if isinstance(payload, dict) else None
        if not isinstance(packages, list):
            raise PatchMonError("PatchMon returned an invalid packages response")
        return [package for package in packages if isinstance(package, dict)]

    async def async_validate(self) -> None:
        """Validate monitoring credentials and optional patch credentials."""
        await self.async_get_hosts()
        if self.patching_enabled:
            await self.async_login()

    async def async_login(self) -> None:
        """Create a user session used by patching endpoints."""
        if not self.patching_enabled:
            raise PatchMonAuthenticationError("Patching credentials are not configured")
        payload = await self._request(
            "POST",
            "/api/v1/auth/login",
            auth=None,
            json={"username": self._username, "password": self._password},
        )
        if payload.get("requiresTfa"):
            raise PatchMonTwoFactorRequired(
                "This PatchMon account requires two-factor authentication"
            )
        token = payload.get("token") or payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise PatchMonAuthenticationError("PatchMon did not return an access token")
        self._access_token = token

    async def async_patch_host(
        self,
        host_id: str,
        *,
        package_names: list[str] | None = None,
        dry_run: bool = False,
        pending_approval: bool = False,
        run_immediately: bool = False,
    ) -> dict[str, Any]:
        """Start a PatchMon patch run for a host."""
        if not self.patching_enabled:
            raise PatchMonAuthenticationError(
                "Configure PatchMon user credentials to enable patching"
            )
        packages = [name.strip() for name in package_names or [] if name.strip()]
        if dry_run and not packages:
            raise PatchMonError(
                "PatchMon supports dry runs only when package_names are supplied"
            )
        body: dict[str, Any] = {
            "host_id": host_id,
            "patch_type": "patch_package" if packages else "patch_all",
            "dry_run": dry_run,
            "pending_approval": pending_approval,
        }
        if packages:
            body["package_names"] = packages
        if run_immediately:
            body["schedule_override"] = "immediate"
        return await self._user_request("POST", "/api/v1/patching/trigger", json=body)

    async def _user_request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Make a user-authenticated request, logging in or retrying once."""
        if not self._access_token:
            await self.async_login()
        try:
            return await self._request(method, path, auth="bearer", **kwargs)
        except PatchMonAuthenticationError:
            self._access_token = None
            await self.async_login()
            return await self._request(method, path, auth="bearer", **kwargs)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        auth: str | None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Make a JSON request and translate transport/API errors."""
        headers = dict(kwargs.pop("headers", {}))
        request_auth = None
        if auth == "basic":
            request_auth = self._basic_auth
        elif auth == "bearer":
            headers["Authorization"] = f"Bearer {self._access_token}"

        try:
            async with asyncio.timeout(30):
                response = await self._session.request(
                    method,
                    f"{self.base_url}{path}",
                    auth=request_auth,
                    headers=headers,
                    ssl=self._verify_ssl,
                    **kwargs,
                )
                return await self._handle_response(response)
        except TimeoutError as err:
            raise PatchMonConnectionError("Timed out connecting to PatchMon") from err
        except ClientError as err:
            raise PatchMonConnectionError(f"Could not connect to PatchMon: {err}") from err

    @staticmethod
    async def _handle_response(response: ClientResponse) -> dict[str, Any]:
        """Decode a PatchMon response and raise useful client exceptions."""
        try:
            payload: Any = await response.json(content_type=None)
        except (ValueError, ClientError):
            payload = {}

        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("error") or payload.get("message") or "")
        message = message or f"PatchMon returned HTTP {response.status}"

        if response.status == HTTPStatus.UNAUTHORIZED:
            raise PatchMonAuthenticationError(message)
        if response.status == HTTPStatus.FORBIDDEN:
            raise PatchMonPermissionError(message)
        if response.status == HTTPStatus.TOO_MANY_REQUESTS:
            raise PatchMonError(f"PatchMon rate limit reached: {message}")
        if response.status >= HTTPStatus.BAD_REQUEST:
            raise PatchMonError(message)
        if not isinstance(payload, dict):
            raise PatchMonError("PatchMon returned a non-object JSON response")
        return payload
