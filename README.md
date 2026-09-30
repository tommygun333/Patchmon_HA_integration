# PatchMon for Home Assistant

A Home Assistant custom integration for a self-hosted [PatchMon](https://github.com/PatchMon/PatchMon) server.

It discovers PatchMon hosts, creates fleet and per-host entities, exposes package update details, and can start PatchMon patch runs from Home Assistant.

## Requirements

- Home Assistant 2024.12 or newer
- PatchMon 2.x
- A PatchMon API credential with the `host:get` scope
- Optional: a local PatchMon username and password with patching permission

PatchMon's scoped Integration API currently provides read access to host and package data. Patch execution uses PatchMon's user-authenticated patching API, so the optional user credentials are required for the patch button and `patchmon.patch_host` action. Accounts that require two-factor authentication cannot be used for unattended patch actions.

## Installation

### HACS custom repository

1. Add this repository to HACS as an **Integration** custom repository.
2. Install **PatchMon**.
3. Restart Home Assistant.

### Manual

Copy `custom_components/patchmon` into your Home Assistant `config/custom_components` directory, then restart Home Assistant.

## PatchMon setup

In PatchMon, open **Settings → Integrations → Auto-Enrollment & API**, create a credential with usage type **API**, and grant it the `host:get` scope. Save the API ID and secret; PatchMon only shows the secret when it creates the credential.

In Home Assistant, open **Settings → Devices & services → Add integration → PatchMon** and enter:

- the PatchMon base URL, such as `https://patchmon.example.com`;
- the scoped API ID and API key;
- optional PatchMon user credentials to enable patching.

## Entities

The PatchMon server device provides fleet totals for hosts, pending updates, security updates, offline hosts, and pending reboots.

Every managed host becomes its own Home Assistant device with:

- available updates;
- security updates;
- installed package count;
- last report time;
- connectivity;
- reboot required;
- **Patch all packages** button when patch credentials are configured.

The available-updates sensor includes package names and versions in its attributes. To protect Home Assistant's recorder, attributes are limited to the first 50 packages. The action below returns the complete live list.
The same sensor exposes the PatchMon host UUID as `host_id` for use in actions.

## Actions

### Query all available package updates

```yaml
action: patchmon.get_host_updates
data:
  host_id: 550e8400-e29b-41d4-a716-446655440000
response_variable: patchmon_updates
```

The response contains `host_id`, `total`, and the full `packages` array from PatchMon.

### Patch all packages on a host

```yaml
action: patchmon.patch_host
data:
  host_id: 550e8400-e29b-41d4-a716-446655440000
```

This respects the effective PatchMon patch policy and schedule. Set `run_immediately: true` to bypass a scheduling delay.

### Patch selected packages

```yaml
action: patchmon.patch_host
data:
  host_id: 550e8400-e29b-41d4-a716-446655440000
  package_names:
    - openssl
    - nginx
```

For selected packages, `dry_run: true` asks PatchMon to validate without installing. Set `pending_approval: true` to create a run that must be approved in PatchMon.

When more than one PatchMon server is configured, add `config_entry_id` to action data. The visual action editor provides a PatchMon server selector.

## Options and API load

The default polling interval is 15 minutes. The integration makes one host-list request per poll, then one package request for each host that has updates when package attributes are enabled. For large fleets, disable package details in the integration options and use `patchmon.get_host_updates` on demand.
