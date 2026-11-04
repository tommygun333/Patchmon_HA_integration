#!/bin/sh
# READ-ONLY. Execute locally on a Debian/Ubuntu/Proxmox/PBS host as root.
# Collect output for a security-only maintenance policy review.
set -eu
printf '\n=== OS identity ===\n'
cat /etc/os-release
printf '\n=== Date / kernel ===\n'
date -Is
uname -a
printf '\n=== Installed Proxmox packages (if any) ===\n'
dpkg-query -W 'proxmox*' 'pve*' 'pbs*' 2>/dev/null || true
printf '\n=== APT configured security origins / allowed origins ===\n'
apt-config dump | grep -Ei 'Unattended-Upgrade::(Allowed-Origins|Origins-Pattern|Automatic-Reboot)|APT::Periodic' || true
printf '\n=== Package sources (may contain mirror names) ===\n'
apt-cache policy
printf '\n=== Upgradable packages (does not install anything) ===\n'
apt list --upgradable 2>/dev/null || true
printf '\n=== Selected Proxmox package candidates ===\n'
apt-cache policy proxmox-ve pve-manager proxmox-backup-server 2>/dev/null || true
printf '\n=== Unattended-upgrades read-only simulation ===\n'
if command -v unattended-upgrade >/dev/null 2>&1; then
  unattended-upgrade --dry-run --debug 2>&1
else
  printf 'unattended-upgrade is not installed; no simulation was run.\n'
fi
printf '\n=== Probe finished (no packages installed) ===\n'
