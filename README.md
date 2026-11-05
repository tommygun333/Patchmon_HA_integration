# PatchMon for Home Assistant

A custom integration for a self-hosted [PatchMon](https://github.com/PatchMon/PatchMon) server. Monitors hosts and outstanding OS package updates, offers optional native per-package Home Assistant update entities, and can launch patching jobs through PatchMon.

## Requirements

- Home Assistant 2026.3+ for bundled integration branding (the integration itself uses conventional HA APIs)
- PatchMon 2.x with a scoped Integration API credential allowing `host:get`
- Optional PatchMon username/password with patching permissions for actionable updates and patch-run status
- Patching functionality in PatchMon must be enabled; a configured policy or schedule may delay installation

**PatchMon PR [#596](https://github.com/PatchMon/PatchMon/pull/596)** proposes long-lived Bearer API tokens for its user-facing endpoints. It is not yet merged. This version continues to use username/password -> login JWT for patch actions; all calls are routed through `PatchMonClient._patch_request()` for an easy future migration. The scoped inventory API ID/key remain separate.

## Installation

### HACS

1. Add this repository as a HACS **Integration** custom repository.
2. Install PatchMon and restart Home Assistant.
3. Go to **Settings -> Devices & services -> Add integration -> PatchMon**.

### Manual

Copy `custom_components/patchmon` into `/config/custom_components/patchmon` and restart Home Assistant.

### Branding

Local brand assets live at `custom_components/patchmon/brand/` and include `icon.png`, `icon@2x.png`, `logo.png`, `logo@2x.png`, `dark_logo.png`, and `dark_logo@2x.png`. Home Assistant 2026.3+ serves them without a brands-repository submission. HACS may still show a generic placeholder; Home Assistant's own Devices & services screen should use the local icon.

## Configuration

In PatchMon, open **Settings -> Integrations -> Auto-Enrollment & API**, create an API credential with usage type **API**, and grant `host:get`. Keep the ID and secret safe. In HA, enter:

- PatchMon server URL, e.g. `https://patchmon.home`
- API ID and API secret for reading host inventory
- Optional user credentials to enable patching and patch-run monitoring (two-factor accounts cannot use non-interactive username/password patching)

### Tracking specific packages on specific hosts

Go to **Settings -> Devices & services -> PatchMon -> Configure -> Watched packages by host**:

1. Select a PatchMon host from the drop-down (names and UUIDs are shown).
2. Enter the exact installed package names separated by commas, e.g. `openssl, nginx`.
3. Save; the integration reloads and adds `update` entities **only for this host's selected packages**.
4. Repeat for other hosts. Clear the package list for a host to stop tracking it.

These entities retain their installed version when they become current, because watched hosts fetch a full package inventory rather than only packages with pending updates. Missing/unreported packages remain unavailable instead of incorrectly appearing up-to-date. Transient API failures similarly mark tracking unavailable rather than inventing a clean state. The `needs_update` flag from PatchMon drives update availability, not a naive semver comparison (Debian/RPM versions often aren't semver).

**Install** on a watched `update` entity queues a package-specific PatchMon run. It does **not** install a package directly from Home Assistant, bypass policy, or imply that a queued run has finished. If patch credentials aren't configured, the update entity is read-only. `update.install` intentionally doesn't support choosing an arbitrary historical version or automatic backups.

### General options

In **Configure -> General settings**, set:

- Polling interval (default 15 minutes, 5-1440 minutes)
- Whether to fetch untracked pending packages for host sensor attributes
- Whether to monitor the **latest patch run** on each host
- Optional host group filter

When enabled, patch-run monitoring exposes a per-host `Last patch run` diagnostic sensor with run ID, status, timestamps, patch type and error. It makes one additional user-authenticated API request per host per polling cycle; disable it for large fleets if you don't need history. The status belongs to an actual PatchMon patch run, *not* an arbitrary agent-queue job.

## Entities

Each host is a device with sensors for available updates, security updates, installed package count and last report timestamp; connectivity and reboot-required binary sensors; and an optional **Patch all packages** button.

Watched packages additionally create `update` entities on that host's device, offering the installed and available versions and a native Install action where authorized. A selected package stops presenting as an outstanding update after PatchMon reports it current.

The PatchMon server device exposes fleet totals for hosts, available updates, security updates, offline hosts and pending reboots.

The available-updates sensor stores up to 50 package entries as attributes to protect Recorder. Use `patchmon.get_host_updates` for the complete live list.

## Actions

Query package updates:

```yaml
action: patchmon.get_host_updates
data:
  host_id: 550e8400-e29b-41d4-a716-446655440000
response_variable: updates
```

Patch an individual package:

```yaml
action: patchmon.patch_host
data:
  host_id: 550e8400-e29b-41d4-a716-446655440000
  package_names:
    - openssl
```

Patch all installed packages allowed by policy:

```yaml
action: patchmon.patch_host
data:
  host_id: 550e8400-e29b-41d4-a716-446655440000
```

`patchmon.patch_host` additionally accepts `dry_run`, `pending_approval` and `run_immediately`. `dry_run` requires a selected package list; `run_immediately` bypasses scheduling delay, not PatchMon policy restrictions. Where multiple PatchMon servers are configured, specify `config_entry_id`.

Install through the native Home Assistant entity action:

```yaml
action: update.install
target:
  entity_id: update.my_server_openssl
```

The exact entity ID depends on your HA host/device name and can be read under **Settings -> Devices & services -> PatchMon**.

### Automatic security update host picker

Go to **Settings → Devices & services → PatchMon → Configure → Automatic security updates**.
Select approved hosts by friendly name in four ordered groups: Proxmox → standard LXCs/VMs → critical hosts → PBS. A host can be selected once. Saving the selection does **not** start any updates.

The weekly `patchmon.security_maintenance` action can omit `host_ids` to use this saved opt-in selection. Explicit `host_ids` still override it for manual calls. Empty or stale automatic selections fail closed. A newly discovered host is never added without your approval. OPNsense is not supported by this APT-oriented workflow.

```yaml
# This is the action only; the full scheduled example is in docs/examples/.
action: patchmon.security_maintenance
data:
  mode: install
  require_backup_checks: true
  backup_checks:
    - entity_id: binary_sensor.patchmon_pbs_ready
      expected_state: "on"
      timestamp_attribute: last_success
      max_age_hours: 6
    - entity_id: binary_sensor.patchmon_vzdump_ready
      expected_state: "on"
      timestamp_attribute: last_success
      max_age_hours: 6
  backup_idle_checks:
    - entity_id: binary_sensor.patchmon_vzdump_idle
      expected_state: "on"
```

Backup entities and `last_success` attributes must exist and be verified before enabling installation. See `docs/examples/weekly_security_automation.yaml`.

## API load and security

- One scoped host-list request per poll.
- One package request for each host with outstanding updates when the host-details option is enabled.
- One full package request for each host that has tracked package names (replacing that host's updates-only request).
- Optional one patch-runs request per host when monitoring is enabled.
- Package and run lookups are limited to five concurrent requests.
- Authentication/permission issues on package/run lookups don't fake empty successful data. Top-level host-list authentication failures surface as HA reauthentication.
- User credentials are currently required for actionable patch runs. No API token from upstream PR #596 is accepted until that support is merged and implemented.

## Development

Run `python -m compileall -q custom_components/patchmon` and `python -m unittest discover -s tests -p 'test_api_v02.py' -v`. Test the config flow, package Install actions and run sensors against a nonproduction PatchMon instance before using the integration for critical machines.

## Security maintenance, reports, and dashboard history

PatchMon's native agent and patch-validation API power both **automatic security-flagged updates** and **manual patch-all preflight**. No PatchMon fork, `unattended-upgrades`, or host-side maintenance script is required.

- **Security maintenance** (`patchmon.security_maintenance`) accepts `mode: plan`, `mode: validate`, or `mode: install`. The install mode validates each host's selected security-flagged packages, approves the successful PatchMon validation run, waits for installation, then checks the refreshed host inventory. Newer candidate versions and required dependencies are allowed. Normal packages are not selected as primary targets. Hosts are processed in the specified order; `stop_on_failure` defaults to `true`.
- **Manual updates** still have **Patch all packages** and **Validate pending updates** buttons. The latter simulates currently pending named packages; it is not an exact Linux `patch_all` simulation because upstream PatchMon does not permit that dry-run type.
- **Persistent history** retains the latest 26 completed install cycles (not merely the most recent notification), latest per-host report, validation/installation IDs, package/version snapshots, errors, health-check results, and reboot flags. Saved data lives in Home Assistant `.storage`, not long Recorder attributes.
- **Dashboard card** supports attention (unread note, expands to details), viewer (newest-first fleet history), and host-specific modes. Acknowledging a report hides its unread indicator, **not** its history.
- **Safety**: configure real backup-success timestamps and backup-idle checks before enabling weekly installation. Missing/stale backups fail closed when `require_backup_checks: true`. No automatic reboot. Interrupted runs are reconciled using PatchMon IDs and never automatically approved a second time.

### Home Assistant examples

Register dashboard JavaScript resource **`/patchmon-security-reports/patchmon-security-report-card.js`** as type **JavaScript module**, then add:

```yaml
# Attention card: add to your dashboard's attention stack
- type: custom:patchmon-security-report-card
  mode: attention
  title: Weekly security maintenance
  hide_when_read: true

# Full fleet history card (always visible)
- type: custom:patchmon-security-report-card
  mode: viewer
  title: Security maintenance history
  limit: 26

# Per-host card (replace with the host's PatchMon UUID)
- type: custom:patchmon-security-report-card
  mode: host
  host_id: "04ed663f-7b47-434e-9408-76b48a45fc22"
  title: Lab security updates
```

For Developer Tools → Actions, run a read-only plan first:

```yaml
action: patchmon.security_maintenance
data:
  host_ids:
    - "YOUR_PATCHMON_HOST_UUID"
  mode: plan
  notify: true
```

The full documentation is in **[Security maintenance and reporting](docs/security-maintenance.md)**, with copyable examples for [dashboard cards](docs/examples/dashboard_cards.yaml), [manual actions](docs/examples/manual_actions.yaml), and the [weekly 03:30 automation](docs/examples/weekly_security_automation.yaml). **The scheduled installation example is disabled until you replace all placeholder UUIDs and configure actual backup and health entities.**

### Run tests and validation

```bash
python3 -m compileall -q custom_components/patchmon
python3 -m unittest discover -s tests -p 'test_*.py' -v
node --check custom_components/patchmon/www/patchmon-security-report-card.js
```

GitHub Actions runs the Python tests on 3.12 and 3.13 plus HACS/hassfest validation. The repository excludes `*.bak` and `__pycache__` artifacts: use Git history rather than committed local backups for rollback.
