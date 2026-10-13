# Applying the PatchMon v0.2.0 changes

**This is a changed-files overlay, not a complete repository.** Do not delete your existing files first. It contains the replacement versions of `api.py`, `config_flow.py`, `const.py`, `coordinator.py`, `sensor.py`, `manifest.json`, `strings.json`, and `README.md`; it adds `update.py`, local PNG branding, and tests. Keep your repository's unchanged `__init__.py`, `button.py`, `binary_sensor.py`, `entity.py`, `services.yaml`, and other files.

## Apply to a local Git checkout

1. Make a feature branch in your local checkout: `git switch -c feature/tracked-package-updates`.
2. Extract the zip at the checkout root, preserving the nested paths. For example, in Windows PowerShell:

   ```powershell
   Expand-Archive -Path "C:\Downloads\patchmon-ha-v0.2.0-overlay.zip" -DestinationPath "C:\src\Patchmon_HA_integration" -Force
   ```

   On Linux:

   ```bash
   unzip -o patchmon-ha-v0.2.0-overlay.zip -d /path/to/Patchmon_HA_integration
   ```

3. Run the included API/entity tests, then test the integration in Home Assistant:

   ```bash
   python -m compileall -q custom_components/patchmon
   python -m unittest discover -s tests -p 'test_*_v02.py' -v
   ```

4. Commit, push and submit a PR after real PatchMon testing. **These changes have not been pushed to GitHub:** the GitHub app rejected creating a branch with HTTP 403.

## Apply to an existing Home Assistant installation for testing

Back up the current `/config/custom_components/patchmon/` directory, copy the overlay's `custom_components/patchmon/*` files into it (overwriting same-named files but keeping other existing files), and restart Home Assistant. Configure per-host tracked packages through Settings > Devices & services > PatchMon > Configure. This is an experimental change; test any actionable Install button on a non-critical machine first.

## Runtime verification checklist

- The integration still authenticates using API ID/secret and optional username/password; nothing uses the unmerged upstream token PR.
- Watch a package already current: its native update entity should be present, without an offered update.
- Watch an outdated package: it should show installed and available versions and offer Install if patch credentials are set.
- Temporarily break package-API permissions: an existing watched update entity should become unavailable, not current.
- Verify an Install action queues `patch_type=patch_package` for the correct host and exact name.
- Enable patch-run monitoring and verify latest queued/running/completed/failed status per host. Confirm Home Assistant reports *queued* operations separately from completed installations.
- Test light/dark integration branding; some HACS versions still show a generic placeholder despite Home Assistant using local assets.
- Check UI on Home Assistant 2026.9+; the supplied unit tests do not substitute for a live HA runtime test.
