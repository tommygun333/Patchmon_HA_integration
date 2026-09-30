"""Config flow for PatchMon."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    PatchMonAuthenticationError,
    PatchMonClient,
    PatchMonConnectionError,
    PatchMonError,
    PatchMonPermissionError,
    PatchMonTwoFactorRequired,
)
from .const import (
    CONF_API_ID,
    CONF_API_KEY,
    CONF_HOST_GROUPS,
    CONF_INCLUDE_PACKAGES,
    CONF_SCAN_INTERVAL,
    DEFAULT_INCLUDE_PACKAGES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)


def _valid_url(value: Any) -> str:
    url = str(value).strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise vol.Invalid("A full HTTP or HTTPS URL is required")
    return url


URL_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.URL, autocomplete="url")
)
TEXT_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD)
)


def _client_from_input(hass: HomeAssistant, data: dict[str, Any]) -> PatchMonClient:
    return PatchMonClient(
        async_get_clientsession(hass),
        data[CONF_URL],
        data[CONF_API_ID],
        data[CONF_API_KEY],
        verify_ssl=data.get(CONF_VERIFY_SSL, True),
        username=data.get(CONF_USERNAME),
        password=data.get(CONF_PASSWORD),
    )


class PatchMonConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle PatchMon configuration."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure a PatchMon server."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                user_input[CONF_URL] = _valid_url(user_input[CONF_URL])
            except vol.Invalid:
                errors[CONF_URL] = "invalid_url"
            if not errors and bool(user_input.get(CONF_USERNAME)) != bool(
                user_input.get(CONF_PASSWORD)
            ):
                errors["base"] = "patch_credentials_incomplete"
            elif not errors:
                client = _client_from_input(self.hass, user_input)
                try:
                    await client.async_validate()
                except PatchMonTwoFactorRequired:
                    errors["base"] = "two_factor_required"
                except PatchMonAuthenticationError:
                    errors["base"] = "invalid_auth"
                except PatchMonPermissionError:
                    errors["base"] = "missing_scope"
                except PatchMonConnectionError:
                    errors["base"] = "cannot_connect"
                except PatchMonError:
                    errors["base"] = "unknown"
                else:
                    await self.async_set_unique_id(client.base_url.lower())
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=urlparse(client.base_url).netloc or "PatchMon",
                        data=user_input,
                    )

        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default="http://patchmon:3000"): URL_SELECTOR,
                vol.Required(CONF_API_ID): TEXT_SELECTOR,
                vol.Required(CONF_API_KEY): PASSWORD_SELECTOR,
                vol.Required(CONF_VERIFY_SSL, default=True): bool,
                vol.Optional(CONF_USERNAME): TEXT_SELECTOR,
                vol.Optional(CONF_PASSWORD): PASSWORD_SELECTOR,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication for an expired scoped API credential."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate replacement scoped API credentials."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            updated_data = {**entry.data, **user_input}
            client = _client_from_input(self.hass, updated_data)
            try:
                await client.async_get_hosts()
            except PatchMonAuthenticationError:
                errors["base"] = "invalid_auth"
            except PatchMonPermissionError:
                errors["base"] = "missing_scope"
            except PatchMonConnectionError:
                errors["base"] = "cannot_connect"
            except PatchMonError:
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(entry, data=updated_data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_API_ID, default=entry.data.get(CONF_API_ID, "")
                    ): TEXT_SELECTOR,
                    vol.Required(CONF_API_KEY): PASSWORD_SELECTOR,
                }
            ),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return PatchMonOptionsFlow()


class PatchMonOptionsFlow(OptionsFlow):
    """Configure PatchMon polling options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage polling and host filtering options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                ),
                vol.Required(
                    CONF_INCLUDE_PACKAGES,
                    default=current.get(CONF_INCLUDE_PACKAGES, DEFAULT_INCLUDE_PACKAGES),
                ): bool,
                vol.Optional(
                    CONF_HOST_GROUPS,
                    default=current.get(CONF_HOST_GROUPS, ""),
                ): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
