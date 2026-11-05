# PatchMon security maintenance: orchestration, reports, and dashboard setup

This document describes the **current Home Assistant integration**. All update execution uses the unmodified upstream PatchMon agent and PatchMon validation/approval API; Home Assistant schedules, monitors, verifies, and stores reports. The older, validation-only rollout and `unattended-upgrades` plan are obsolete.

## Policy and safety

For each specified host, the integration selects only packages for which PatchMon reports `is_security_update: true`. PatchMon then runs a native named-package dry-run. In `mode: install`, a successful validation is automatically approved and the ensuing installation is monitored and verified with a fresh PatchMon inventory report. Newer candidate versions and required dependency upgrades are allowed; this is **not** a security-repository allowlist. Regular packages remain manual primary targets.

- Modes: `plan` (inventory only), `validate` (PatchMon dry-run only), and `install` (validate → approve → install → verify).
- One active cycle per PatchMon configuration; hosts processed in listed order. Use `stop_on_failure: true` so a failed host stops subsequent updates.
- Zero security packages means `no_security_updates`, not a fallback to `patch_all`.
- No automatic reboots; report pending reboot status for review. The Proxmox host is a single point of failure, so plan reboot windows separately.
- The automation requires a **real** successful-backup timestamp and backup-idle check when `require_backup_checks: true`. It cannot infer backup success solely because it is 03:30 or HA has recently restarted.
- OPNsense firmware is outside this Linux selected-package workflow unless separately validated; don't automatically include it in the fleet list.
- A service-health check is based on your selected HA entities, e.g. Uptime Kuma connectivity sensors. Without configured checks the report must not imply services were health-verified.

## Setup

1. Install the integration via HACS (or copy `custom_components/patchmon/` into HA), configure a PatchMon inventory API ID/key and an authorized user for patch actions, then restart Home Assistant.
2. Enable **Last patch run** monitoring in PatchMon integration options, as appropriate for your host count.
3. In **Settings → Dashboards → Resources**, register `/patchmon-security-reports/patchmon-security-report-card.js` with resource type **JavaScript module**. The integration serves the file; you do not have to copy it into HA `/config/www`.
4. Add the appropriate card to your existing dashboard. The card's modes are `attention`, `viewer`, and `host`. See [dashboard_cards.yaml](examples/dashboard_cards.yaml).
5. Test a single host in `plan` and `validate` modes before `install`. For unattended weekly installation, replace all example host IDs and configure trustworthy backup and service health entities.

## Dashboard cards

```yaml
# Add inside your existing dashboard's attention stack
# Clicking an unread note expands the latest full report and historical list.
type: custom:patchmon-security-report-card
mode: attention
title: Weekly security maintenance
report_source: automation
```

```yaml
# Always-visible report browser, newest first
type: custom:patchmon-security-report-card
mode: viewer
title: Security maintenance history
limit: 26
```

```yaml
# For a specific host (PatchMon UUID, not Proxmox VM/LXC ID)
type: custom:patchmon-security-report-card
mode: host
host_id: "04ed663f-7b47-434e-9408-76b48a45fc22"
title: Lab security update history
```

```yaml
# Compact per-host launcher using Home Assistant's native dialog manager
type: custom:patchmon-security-report-card
mode: host
display: dialog
host_id: "04ed663f-7b47-434e-9408-76b48a45fc22"
title: Lab update reports
```

Use Home Assistant's built-in card visibility with the **Automatic security maintenance report unread** binary sensor when the attention card should disappear after acknowledgment. The legacy `hide_when_read` option remains accepted but no longer hides the card internally. The cards do not depend on entity-name guesses; look up generated entity IDs in **Developer Tools → States**. See [dashboard_cards.yaml](examples/dashboard_cards.yaml) for additional ready-to-copy layouts.

## Stored reports and entities

The integration retains up to **26 automatic reports and 26 manual validation reports** per config entry, newest first. Every report has a `report_id`, `report_source: automation | manual`, type/mode, start/end timestamps, status, summary counts, and host entries using actual PatchMon UUIDs. Host entries can include validation and installation IDs, before/after versions, dependency/removal details, bounded validation console output, health results, errors, remaining packages, and reboot requirements. History persists in Home Assistant `.storage` across restarts. Previous unpersisted test responses are not imported retroactively.

Diagnostic entities include a fleet-level **Last security maintenance** sensor, the existing compatibility unread sensor, a separate stable **Automatic security maintenance report unread** binary sensor, and a **Last security maintenance** sensor per host. Both unread binary sensors are informational On/Off indicators without the `problem` device class, and both follow automatic reports only; manual Patch-all validations never turn them on. These entities carry only compact attributes; full reports are fetched on demand. `patchmon.acknowledge_automation_security_report` clears automatic attention, while the existing acknowledgment remains available for mixed report history. Interrupted cycles remain separate for reconciliation.

Actions available under **Developer Tools → Actions**:

```yaml
# One independent action to view stored reports
# action: patchmon.get_security_reports
# data:
#   limit: 26

# Filter history by host:
# action: patchmon.get_security_reports
# data:
#   host_id: "04ed663f-7b47-434e-9408-76b48a45fc22"
#   limit: 26

# Retrieve the complete report from one entry in that list:
# action: patchmon.get_security_reports
# data:
#   report_id: "PASTE_REPORT_ID_HERE"

# Acknowledge a report without deleting history:
# action: patchmon.acknowledge_security_report
# data:
#   report_id: "PASTE_REPORT_ID_HERE"

# Acknowledge only the latest automatic report:
# action: patchmon.acknowledge_automation_security_report
# data:
#   report_id: "PASTE_AUTOMATION_REPORT_ID_HERE"
```

Every completed automatic-source run creates its own persistent alert, including zero-update, blocked, failed, and recovered outcomes. Its **View report** link opens the exact report at `/patchmon-reports/<config_entry_id>/<report_id>`; the hidden panel has no sidebar entry.

Actions `patchmon.reconcile_security_reports` and `patchmon.resolve_interrupted_security_cycle` support post-restart investigation. Reconciliation reads existing PatchMon run IDs; it must never automatically approve a previously interrupted validation. Only resolve an interrupted cycle after confirming no patch jobs remain active.

## Manual validation before full updates

The per-host **Validate pending updates** button submits the currently pending named package list as a PatchMon dry-run and persists a `report_source: manual` report for successful, empty, blocked, and failed results. Console evidence is bounded to 4096 characters/40 lines. The original **Patch all packages** button remains available and unchanged. Linux PatchMon does not support a *native patch-all dry-run*, so this is not an exact full-upgrade simulation. The validation action never approves or installs packages.

## Scheduled weekly 03:30 example

The supplied [weekly_security_automation.yaml](examples/weekly_security_automation.yaml) is intentionally `enabled: false`. Update the YAML with your actual ordered PatchMon host UUIDs, backups, and health checks. Schedule Proxmox first after completed backups, followed by noncritical LXCs, critical LXCs, then PBS. Do **not** assume the listed times correspond to immediate starts of later hosts: each stage waits for completion.

Example action data after substituting your own entities:

```yaml
action: patchmon.security_maintenance
data:
  report_source: automation
  host_ids:
    - "PROXMOX_PATCHMON_UUID"
    - "NONCRITICAL_LXC_PATCHMON_UUID"
    - "CRITICAL_LXC_PATCHMON_UUID"
    - "PBS_PATCHMON_UUID"
  mode: install
  stop_on_failure: true
  require_backup_checks: true
  backup_checks:
    - entity_id: binary_sensor.proxmox_backup_success
      expected_state: "on"
      timestamp_attribute: last_success
      max_age_hours: 30
  backup_idle_checks:
    - entity_id: binary_sensor.proxmox_backup_running
      expected_state: "off"
  health_checks:
    "CRITICAL_LXC_PATCHMON_UUID":
      - entity_id: binary_sensor.critical_service_online
        expected_state: "on"
  notify: true
```

**The entity IDs above are placeholders, not claims about your HA configuration.** Each success entity must contain a real timezone-aware completion timestamp, e.g. `2026-11-04T02:15:00+01:00`, and each idle sensor must accurately reflect running backup jobs. The backup gate checks age and fails closed when absent or stale. Health checks are optional but valuable for DNS/reverse proxy/authentication hosts; choose corresponding real entities from your monitoring setup.

## Testing and troubleshooting

Use [manual_actions.yaml](examples/manual_actions.yaml) for copyable plan/validate/install and reporting actions. Start with one recently updated test LXC to verify `no_security_updates`, an unread note, report retrieval, acknowledgement, and history retention across a HA restart. Then validate two hosts in the desired order, one of which has pending security updates. No need to deliberately interrupt a live package install to test recovery.

```bash
python3 -m compileall -q custom_components/patchmon
python3 -m unittest discover -s tests -p 'test_*.py' -v
node --check custom_components/patchmon/www/patchmon-security-report-card.js
```

For missing `patchmon.get_security_reports`, check the integration reload and HA logs. If the JavaScript card doesn't render, check browser cache and the resource URL in Dashboard Resources. For an offline or stale host, check its last PatchMon inventory report. For blocked installations inspect the detailed report's backup preflight failures, validation status and post-health status. **The next report after an update should show original selected versions in its before snapshot and current versions in verification/after snapshot.**


## Automatic host selection (v0.2.5)

In PatchMon integration options, open **Automatic security updates** and select hosts by name in the stages **Proxmox**, **standard**, **critical**, and **PBS**. Hosts run in stage order, alphabetically by name within a stage. Each host must be selected at most once, and newly discovered hosts are not enrolled automatically.

The `patchmon.security_maintenance` action can now omit `host_ids` to use the saved list; manual calls with explicit `host_ids` still override it. Missing selections or inventory hosts block the call before any installation. For your 03:10 VZDump/PBS backups and PBS verification around 03:43, the example starts at **03:50 Sunday**, waits up to 20 minutes, then always executes the PatchMon preflight. If prerequisites remain unsatisfied, no packages are installed and a blocked cycle report is written. The backup-status templates must be tested and refreshed in HA before enabling the automation.
