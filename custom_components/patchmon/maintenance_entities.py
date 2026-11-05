"""Small HA report entities. Full report details are stored separately, not in Recorder."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory

from .entity import PatchMonHostEntity, hub_device_info


class _ReportListener:
    """Subscribe to history writes independently from periodic PatchMon inventory."""

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.maintenance_reports.subscribe(self.async_write_ha_state)
        )


class PatchMonSecurityCycleSensor(_ReportListener, SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Last security maintenance"
    _attr_icon = "mdi:shield-check"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_last_security_maintenance"

    @property
    def device_info(self):
        return hub_device_info(self.coordinator)

    @property
    def native_value(self):
        latest = self.coordinator.maintenance_reports.latest(report_source="automation")
        return latest.get("status", "unknown") if latest else "never"

    @property
    def extra_state_attributes(self):
        history = self.coordinator.maintenance_reports
        latest = history.latest(report_source="automation")
        if not latest:
            return {"unread": False, "report_count": 0}
        return {
            "report_id": latest.get("report_id"),
            "report_source": latest.get("report_source", "automation"),
            "report_type": latest.get("report_type", "security_maintenance"),
            "started_at": latest.get("started_at"),
            "completed_at": latest.get("completed_at"),
            "mode": latest.get("mode"),
            "unread": history.unread,
            "report_count": len(history.list(report_source="automation")),
            "summary": latest.get("summary", {}),
        }


class PatchMonSecurityReportUnread(_ReportListener, BinarySensorEntity):
    """Compatibility unread indicator for automatic maintenance reports only."""

    _attr_has_entity_name = True
    _attr_name = "Security maintenance report unread"
    _attr_icon = "mdi:shield-alert-outline"

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_security_report_unread"

    @property
    def device_info(self):
        return hub_device_info(self.coordinator)

    @property
    def is_on(self):
        return self.coordinator.maintenance_reports.automation_unread

    @property
    def extra_state_attributes(self):
        latest = self.coordinator.maintenance_reports.latest(
            report_source="automation"
        )
        return {
            "report_id": latest.get("report_id") if latest else None,
            "report_source": "automation",
        }


class PatchMonAutomationReportUnread(_ReportListener, BinarySensorEntity):
    """Stable alert entity for completed automatic maintenance reports only."""

    _attr_has_entity_name = True
    _attr_translation_key = "automation_security_report_unread"
    _attr_icon = "mdi:shield-sync-outline"

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_automation_security_report_unread"
        )

    @property
    def device_info(self):
        return hub_device_info(self.coordinator)

    @property
    def is_on(self):
        return self.coordinator.maintenance_reports.automation_unread

    @property
    def extra_state_attributes(self):
        latest = self.coordinator.maintenance_reports.latest(
            report_source="automation"
        )
        return {
            "report_id": latest.get("report_id") if latest else None,
            "report_source": "automation",
        }


class PatchMonHostSecurityCycleSensor(_ReportListener, PatchMonHostEntity, SensorEntity):
    """Latest saved weekly security-maintenance result for each PatchMon host."""

    _attr_name = "Last security maintenance"
    _attr_icon = "mdi:shield-check-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, host_id):
        super().__init__(coordinator, host_id)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{host_id}_security_maintenance"

    @property
    def native_value(self):
        match = self.coordinator.maintenance_reports.latest_host(
            self.host_id, report_source="automation"
        )
        return match[1].get("state", "unknown") if match else "never"

    @property
    def extra_state_attributes(self):
        match = self.coordinator.maintenance_reports.latest_host(
            self.host_id, report_source="automation"
        )
        if not match:
            return {"host_id": self.host_id}
        report, host = match
        return {
            "report_id": report.get("report_id"),
            "report_source": report.get("report_source", "automation"),
            "report_type": report.get("report_type", "security_maintenance"),
            "date": report.get("completed_at"),
            "host_id": self.host_id,
            "selected_security_packages": host.get("security_count", 0),
            "validation_run_id": (host.get("validation") or {}).get("run_id"),
            "installation_run_id": (host.get("installation") or {}).get("run_id"),
            "reboot_required": bool((host.get("verification") or {}).get("reboot_required")
                                    or host.get("reboot_required")),
            "error": str(host.get("error") or host.get("reason") or "")[:200],
            "pre_health": (host.get("pre_health") or {}).get("status"),
            "post_health": (host.get("post_health") or {}).get("status"),
        }
