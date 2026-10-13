"""Constants for PatchMon."""

from homeassistant.const import Platform

DOMAIN = "patchmon"
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON, Platform.UPDATE]

CONF_API_ID = "api_id"
CONF_API_KEY = "api_key"
CONF_HOST_GROUPS = "host_groups"
CONF_INCLUDE_PACKAGES = "include_packages"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_TRACKED_PACKAGES = "tracked_packages"
CONF_MONITOR_PATCH_RUNS = "monitor_patch_runs"

DEFAULT_SCAN_INTERVAL = 15
DEFAULT_INCLUDE_PACKAGES = True
DEFAULT_MONITOR_PATCH_RUNS = False
MIN_SCAN_INTERVAL = 5
MAX_SCAN_INTERVAL = 1440
PACKAGE_ATTRIBUTE_LIMIT = 50

SERVICE_PATCH_HOST = "patch_host"
SERVICE_GET_HOST_UPDATES = "get_host_updates"
ATTR_HOST_ID = "host_id"
ATTR_PACKAGE_NAMES = "package_names"
ATTR_DRY_RUN = "dry_run"
ATTR_PENDING_APPROVAL = "pending_approval"
ATTR_RUN_IMMEDIATELY = "run_immediately"
