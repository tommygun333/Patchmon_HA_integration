# Security-only maintenance: validation-first rollout

**Status: validation-only. No installation is implemented or authorized by this
workflow.** It is intended to be a safe first rollout before automating actual
security-repository-restricted upgrades on each supported host.

## Why this matters

PatchMon distinguishes `is_security_update` in its package inventory, but its
Linux `patch_package` command ultimately uses APT's **latest candidate** for a
requested package. Selecting only security-flagged package names therefore does
**not** guarantee security-only installation: regular-origin versions and
additional dependencies can be installed. A successful PatchMon dry-run is
informative, not authorization to execute that PatchMon patch run. In
particular, **do not approve** a validation run from this workflow in PatchMon.

On Debian/Ubuntu guests, we plan to use a separately verified
`unattended-upgrades` configuration that allows only security origins. Proxmox
VE and Proxmox Backup Server require explicit repository-aware validation
before any automatic execution. OPNsense firmware should stay outside this
APT-based workflow and use its own updater in a separately controlled window.

## New Home Assistant action

`patchmon.security_maintenance` returns a structured response. It can run in
read-only planning mode or trigger **PatchMon dry-run validation only**.

```yaml
- action: patchmon.security_maintenance
  data:
    host_ids:
      - "REPLACE_WITH_PROXMOX_PATCHMON_UUID"
      - "REPLACE_WITH_NONCRITICAL_LXC_PATCHMON_UUID"
      - "REPLACE_WITH_CRITICAL_LXC_PATCHMON_UUID"
      - "REPLACE_WITH_PBS_PATCHMON_UUID"
    validate: true
    max_report_age_minutes: 120
    notify: true
  response_variable: security_report
```

All UUIDs must correspond to hosts in the selected PatchMon instance. The
selected order is preserved. The action checks the full live pending-package
list, not the truncated set in sensor attributes. It only selects packages with
`is_security_update is True`, checks host inventory freshness and connectivity,
and rejects inconsistent package counts, malformed names or batches exceeding
PatchMon's 100-package limit. Empty security selections are skipped and NEVER
become `patch_all` calls. It creates one PatchMon dry-run for each eligible host
and waits for its terminal status. Regular updates are not submitted.

A validation run may identify additional dependencies. The response preserves
those names when upstream exposes them. Regardless of dry-run success,
`execution_safe` is always `false` because PatchMon's installation path is not
security-origin restricted.

The response includes `summary`, `hosts`, `started_at`, `completed_at`, and
`installed_packages: 0`. It emits a
`patchmon_security_maintenance_report` HA event and, when `notify: true`,
updates a persistent notification named "PatchMon security maintenance
report". You can inspect the per-host plan from the action response. Note that
persistent notifications are replaced on each run; retain HA events or the
response in a long-term log if you need a full audit trail.

## Example weekly validation automation (initially disabled)

```yaml
- id: patchmon_security_validation
  alias: PatchMon - weekly security validation
  enabled: false  # Enable after replacing the UUIDs below.
  mode: single
  triggers:
    - trigger: time
      at: "03:30:00"
  conditions:
    - condition: time
      weekday:
        - sun
    # Before enabling actual installation, add a real PBS/PVE backup-job
    # success and no-active-backup-task gate. Clock time alone is not enough.
  actions:
    - action: patchmon.security_maintenance
      data:
        host_ids:
          - "REPLACE_WITH_PROXMOX_PATCHMON_UUID"
          - "REPLACE_WITH_FIRST_LXC_PATCHMON_UUID"
          - "REPLACE_WITH_PBS_PATCHMON_UUID"
        validate: true
        notify: true
      response_variable: security_report
    - if:
        - condition: template
          value_template: >-
            {{ security_report.summary.blocked > 0 or
               security_report.summary.failed_validation > 0 }}
      then:
        - action: persistent_notification.create
          data:
            title: PatchMon security validation needs attention
            message: >-
              {{ security_report.summary.blocked }} blocked hosts and
              {{ security_report.summary.failed_validation }} failed validation runs.
              See the PatchMon security maintenance report.
```

The script above **does not patch hosts**. Only unattended validation is
scheduled. If the user expects real security-only installation at 03:30, that
must be implemented *after* validating a safe host-side security updater and
backup gate; do not substitute `patchmon.patch_host` without such a restriction.

## Read-only on-host probe

Run the included `docs/security-apt-probe.sh` manually as root on Proxmox, PBS
and one Debian/Ubuntu LXC. It reports package-manager origin rules, active
repositories, pending updates, and runs `unattended-upgrade --dry-run --debug`
if the binary is installed. It does not install or remove anything.

```sh
sudo bash docs/security-apt-probe.sh > /tmp/security-apt-probe.txt 2>&1
```

Review the result for distro release/codename, `Unattended-Upgrade` allowed
origins, source components, Proxmox packages and any regular-origin candidates
in the simulated plan. `unattended-upgrades` may already be configured for
regular updates; this probe **does not trust** that configuration.

Before real execution can be added, we need a trusted security-only executor
per guest, a backup completion check, an explicit no-reboot policy, reliable
job completion reporting, and an execution audit. Keep Proxmox/PBS and
OPNsense excluded until we validate their own native update behavior.

## Verify the patch

```sh
python -m compileall -q custom_components/patchmon
python -m unittest discover -s tests -p 'test_security_maintenance.py' -v
```

As a smoke test in HA, call `patchmon.security_maintenance` with one host and
`validate: false` first. Then use `validate: true` to create a dry-run and
compare its candidates to the local host probe. Do not approve that validation
in PatchMon, since approval could run its unrestricted APT upgrade path.
