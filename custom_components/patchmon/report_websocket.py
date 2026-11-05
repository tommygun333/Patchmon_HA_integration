"""Authenticated Home Assistant WebSocket history endpoint for Lovelace report cards."""
from __future__ import annotations

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback

from .const import DOMAIN


def _history(hass, msg):
    entries = [e for e in hass.config_entries.async_entries(DOMAIN)
               if e.state is ConfigEntryState.LOADED]
    selected = msg.get("config_entry_id")
    if selected:
        entries = [e for e in entries if e.entry_id == selected]
    if len(entries) != 1:
        raise ValueError("Select a loaded PatchMon config_entry_id")
    return entries[0].runtime_data.maintenance_reports


@websocket_api.websocket_command({
    vol.Required("type"): "patchmon/security_reports",
    vol.Optional("config_entry_id"): str,
    vol.Optional("report_id"): str,
    vol.Optional("host_id"): str,
    vol.Optional("report_source"): vol.In(("automation", "manual")),
    vol.Optional("limit", default=26): vol.All(int, vol.Range(min=1, max=26)),
})
@callback
def ws_reports(hass, connection, msg):
    try:
        history = _history(hass, msg)
        rid = msg.get("report_id")
        if rid:
            report = history.get(rid)
            if report is None:
                connection.send_error(msg["id"], "not_found", "Security report not found")
                return
            connection.send_result(msg["id"], {
                "report": report,
                "unread": history.unread,
                "automation_unread": history.automation_unread,
            })
            return
        connection.send_result(msg["id"], {
            "reports": history.list(
                limit=msg["limit"], host_id=msg.get("host_id"),
                report_source=msg.get("report_source"),
            ),
            "unread": history.unread,
            "automation_unread": history.automation_unread,
            "active": history.active,
            "retained": history.limit,
        })
    except ValueError as exc:
        connection.send_error(msg["id"], "invalid_config", str(exc))


@websocket_api.websocket_command({
    vol.Required("type"): "patchmon/security_report_ack",
    vol.Optional("config_entry_id"): str,
    vol.Optional("report_id"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_ack(hass, connection, msg):
    try:
        history = _history(hass, msg)
        rid = await history.acknowledge(msg.get("report_id"))
        connection.send_result(msg["id"], {"acknowledged_report_id": rid})
    except ValueError as exc:
        connection.send_error(msg["id"], "invalid_report", str(exc))


@websocket_api.websocket_command({
    vol.Required("type"): "patchmon/automation_security_report_ack",
    vol.Optional("config_entry_id"): str,
    vol.Optional("report_id"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_automation_ack(hass, connection, msg):
    try:
        history = _history(hass, msg)
        rid = await history.acknowledge_automation(msg.get("report_id"))
        connection.send_result(msg["id"], {
            "acknowledged_report_id": rid,
            "automation_unread": history.automation_unread,
        })
    except ValueError as exc:
        connection.send_error(msg["id"], "invalid_report", str(exc))


def register_websocket(hass):
    websocket_api.async_register_command(hass, ws_reports)
    websocket_api.async_register_command(hass, ws_ack)
    websocket_api.async_register_command(hass, ws_automation_ack)
