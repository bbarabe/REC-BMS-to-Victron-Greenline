# archive/ — retired Node-RED flows

**Nothing in this folder is deployed, and nothing here should be deployed as-is.**
Every flow the system ever ran was replaced by a standalone D-Bus driver during
August 2026. They are kept only so a rollback does not have to go through git
history.

With `InstanceRegistry.json` retired there are **no active Node-RED flows left**:
nothing on the boat depends on Signal K or Node-RED any more.

| File | Replaced by | Retired |
|---|---|---|
| `Virtual BMS.json` | [`../dbus-recbms/`](../dbus-recbms/) | 2026-08-17 |
| `CZoneProxy.json` | [`../dbus-czone/`](../dbus-czone/) | 2026-08-18 |
| `SolarPriority.json` (v4.2) | [`../dbus-recbms/solar_priority.py`](../dbus-recbms/) (`dbus-solarpriority`) | 2026-08-21 |
| `BatteriesForward.json` | [`../dbus-batteries/`](../dbus-batteries/) | 2026-08-21 |
| `GreenlineEDriveFlow.json` | [`../dbus-edrive/`](../dbus-edrive/) | 2026-08-21 |
| `InstanceRegistry.json` | the drivers themselves | 2026-08-21 |

## Read this before importing any of them

- **Never run a flow here alongside its driver.** They claim the same D-Bus
  resources and will fight:
  - `Virtual BMS.json` and `dbus-recbms` both publish instances **200** and
    **220**.
  - `CZoneProxy.json` and `dbus-czone` are both writers on the **same CZone
    bank-1 circuits**; two writers on one bank produce wrong latch states.
  - `SolarPriority.json` and `dbus-solarpriority` both write the Quattro's
    **`/Ac/Control/IgnoreAcIn1`** and both claim instance **221**; two
    controllers on the AC input fight each other into FAULT lockouts.
  - `BatteriesForward.json` and `dbus-batteries` both claim instances
    **201-205**.
  - `GreenlineEDriveFlow.json` and `dbus-edrive` both claim instances
    **210/211**. The flow also starts a `candump` subprocess; the driver uses
    kernel filters and starts nothing.
  Stop and uninstall the driver (`uninstall.sh`) *before* importing.
- **The behaviour described in `specification.md` is the drivers', not these
  flows'.** Every fix and calibration since each retirement date exists only in
  the driver: the piecewise CVL curve, the solar lead and boost, the CZone
  bank-1 press/release control model, the settings-driven battery selection.
  `Virtual BMS.json` in particular predates the 2026-08-19 CVL calibration and
  the cold-boot startup-grace fix.

## Why `InstanceRegistry.json` could be retired

The registry existed because the Node-RED victron palette derives a virtual
device's D-Bus service name from the node id and lets localsettings
auto-allocate the instance — so any regenerated node id minted a new service and
a new instance, and Signal K kept the orphan paths from every generation.

No `virtual_*` device is published any more. Each driver now owns the numbers it
uses and pins them itself, through a settings id **without** the `virtual_`
prefix (so the palette's auto-cleanup cannot touch it):

| Instance | Class | Service | Owner |
|---|---|---|---|
| 200 | battery | `battery.recbms` | `dbus-recbms` |
| 201-205 | battery | `battery.n2kbat_*` | `dbus-batteries` |
| 206-209 | battery | — | `dbus-batteries` spare pool |
| 210/211 | motordrive | `motordrive.edrive_port` / `edrive_stbd` | `dbus-edrive` |
| 220 | switch | `switch.recbms_maxcharge` | `dbus-recbms` |
| 221 | switch | `switch.solarpriority` | `dbus-solarpriority` |
| 224 | switch | `switch.czone` | `dbus-czone` |
| 222, 225-235 | — | *(retired, never reused)* | — |

The never-reuse rule survives the registry: `dbus-batteries` allocates from its
pool monotonically through `/Settings/N2kBatteries/NextInstance`.

`verify_pinning.py` still works and is still worth running after a Venus
firmware update, but its Node-RED restart loop no longer proves anything — the
drivers do not restart with Node-RED.

## Rollback procedure

1. `sh /data/dbus-<name>/uninstall.sh` on the Cerbo.
2. Clear the driver's settings entries if you want a clean re-registration:
   `/Settings/Devices/recbms`, `/Settings/Devices/czone`,
   `/Settings/Devices/solarpriority`, `/Settings/Devices/n2kbat_*`,
   `/Settings/Devices/edrive_*`.
3. Import the JSON from this folder into Node-RED and deploy.
4. Any flow with virtual devices also needs `InstanceRegistry.json` imported and
   *Enforce + audit now* run, or its devices register on auto-assigned instances
   and litter the Signal K data model. For `CZoneProxy.json`, first restore the
   `cz_vs_sw1`-`cz_vs_sw11` rows in the registry (they are in git history,
   commit `d00780d` and earlier).
