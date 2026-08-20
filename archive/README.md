# archive/ — retired Node-RED flows

**Nothing in this folder is deployed, and nothing here should be deployed as-is.**
Both flows were replaced by standalone D-Bus drivers in August 2026. They are
kept only so a rollback does not have to go through git history.

| File | Replaced by | Retired |
|---|---|---|
| `Virtual BMS.json` | [`../dbus-recbms/`](../dbus-recbms/) | 2026-08-17 |
| `CZoneProxy.json` | [`../dbus-czone/`](../dbus-czone/) | 2026-08-18 |

## Read this before importing either one

- **Never run a flow here alongside its driver.** They claim the same D-Bus
  resources and will fight:
  - `Virtual BMS.json` and `dbus-recbms` both publish instances **200** and
    **220**.
  - `CZoneProxy.json` and `dbus-czone` are both writers on the **same CZone
    bank-1 circuits**; two writers on one bank produce wrong latch states.
  Stop and uninstall the driver (`uninstall.sh`) *before* importing.
- **The Instance Registry no longer pins these flows' devices.**
  `CZoneProxy.json`'s eleven virtual switches (instances **225-235**) were
  removed from the registry in [`../InstanceRegistry.json`](../InstanceRegistry.json)
  when `dbus-czone` took over instance 224. Re-importing the flow means
  restoring those rows first, or its devices will register on auto-assigned
  instances and litter the Signal K data model.
- **The behaviour described here is frozen at the retirement date.** Every fix
  and calibration since then — the piecewise CVL curve, the solar lead, the
  solar boost, the CZone bank-1 press/release control model — exists only in
  the drivers. `Virtual BMS.json` in particular predates the 2026-08-19 CVL
  calibration and the cold-boot startup-grace fix.
- The system documentation in [`../specification.md`](../specification.md)
  describes the **drivers**, not these flows.

## Rollback procedure

1. `sh /data/dbus-<name>/uninstall.sh` on the Cerbo.
2. Clear the driver's settings entry if you want a clean re-registration
   (`/Settings/Devices/recbms`, `/Settings/Devices/czone`).
3. Import the JSON from this folder into Node-RED and deploy.
4. For `CZoneProxy.json`, restore the `cz_vs_sw1`-`cz_vs_sw11` rows in the
   Instance Registry (they are in git history, commit `d00780d` and earlier),
   deploy it, and run *Enforce + audit now*.
