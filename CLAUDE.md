# CLAUDE.md

Read `specification.md` for a full description of the system architecture, CAN bus
topology, battery systems, YDNB-07 bridge configuration, and Node-Red flows.

---

# Deploying to the Cerbo

The Cerbo GX is named **`einstein`** (Venus OS, armv7l). `venus.local` does **not**
resolve from the dev machine — use the IP. The host and root password come from the
environment, never from a file in this repo:

```sh
export CERBO_HOST=<cerbo-ip>      # not stored here: this repo is public
export CERBO_PASS=<root-password>
```

All helper scripts (`cerbo_ssh.py`, `nmea_capture.py`, `verify_pinning.py`,
`edrive_temps.py`) read those two variables. `cerbo_ssh.py` is the read-only
command runner: `python cerbo_ssh.py "svstat /service/dbus-recbms"`.

**Use one SSH session and keep it alive.** The Cerbo is a small armv7l box with
little SSH headroom: opening a connection per command — or polling port 22 — exhausts
it, and it then stops answering *entirely* for a good while. Connect once, run every
command of the task over that one transport (each `exec_command` is a cheap extra
channel; open SFTP from the same client), and set a 30s keepalive. A failed connect
means stop and wait, never retry in a loop — the retries are what cause the outage.

Because a session can still be lost mid-task, make each step individually
verifiable: re-check `grep -m1 '^VERSION'` after a restart rather than assuming the
copy landed.

## Two deploy paths, do not mix them

| What | How | Who |
|---|---|---|
| `dbus-recbms/`, `dbus-czone/` | `scp` to `/data/`, then `svc -t` | scriptable |
| `*.json` Node-RED flows | import + Deploy in the Node-RED UI | **the user, by hand** |

**Never hot-deploy a flow** — do not splice `flows_einstein.json` on the Cerbo and
do not restart Signal K to force a flow reload. Finish flow work in the repo and
hand it over. Node-Red is *embedded in the Signal K server*, so restarting Node-Red
means restarting Signal K and everything else it hosts.

## Standalone drivers

```sh
scp -r dbus-recbms root@$CERBO_HOST:/data/
ssh root@$CERBO_HOST 'svc -t /service/dbus-recbms'      # ~2s outage
```

`install.sh` is only for a **first** install (it creates `/service/<name>` and the
`/data/rc.local` hook that survives firmware updates). An already-installed driver
just needs the copy and the restart.

> **`scp -r` overwrites `config.ini`.** It holds the boat's calibration — the CVL
> curve, `solar_lead_v`, the solar-boost clamps, `momentary_outputs`. Diff the live
> copy against the repo *before* copying, and fold any on-boat edits back into the
> repo first:
> ```sh
> ssh root@$CERBO_HOST 'cat /data/dbus-recbms/config.ini' > /tmp/boat.ini
> diff /tmp/boat.ini dbus-recbms/config.ini
> ```

Verify:

```sh
svstat /service/dbus-recbms                              # "up (pid N) Ns"
grep -m1 '^VERSION' /data/dbus-recbms/dbus_recbms.py     # the version you shipped
tail -f /var/log/dbus-recbms/current | tai64nlocal
dbus -y com.victronenergy.battery.recbms / GetValue
```

Restart: `svc -t /service/<name>`. Stop: `svc -d`. Rollback: `uninstall.sh`, then
re-import the matching flow from `archive/` (read `archive/README.md` first — the
flows and the drivers claim the same instances and must never run together).

## Node-RED flows

Hand the `.json` to the user to import and Deploy. After deploying anything that
publishes a virtual device:

1. `InstanceRegistry.json` -> **Enforce + audit now**; expect no `RECONVERGE`.
2. Orphan `/Settings/Devices/virtual_*` entries -> **Remove ORPHAN settings**.
3. `svc -t /service/signalk-server` so Signal K drops cached dead paths.
4. `python verify_pinning.py` — restarts Node-RED 3× and fails if any device
   instance moves or a new Signal K path appears.

Every virtual device needs a row in the registry node *before* it is deployed, with
a hand-written, dot-free node ID. A device retired from the boat must have its row
**removed** — leaving it there re-creates its settings entry on every enforcement
pass and hides it from the orphan audit.

## DVCC access level

The solar lead and boost ride on systemcalc's `/Debug/BatteryOperationalLimits/
SolarVoltageOffset`, which dvcc.py applies **only while the GX access level is
Superuser** (Settings → General → Access level), evaluated once per systemcalc
process — though the boat's current firmware applied it at level 2, so the
gate is version-dependent. After changing the level run
`svc -t /service/dbus-systemcalc-py`. dbus-recbms ≥ 1.4.0 detects the offset
being ignored (`/RecBms/LeadFault`, InternalFailure warning) and falls back to
the full target — so a lead fault after a settings reset is a symptom to fix,
not a driver bug.

## Deploy order when a flow depends on a driver path

Driver first, always. A flow that reads a D-Bus path the running driver does not
publish yet will silently sit on missing data (e.g. `SolarPriority.json` needs
`/RecBms/TargetChargeVoltage`, which only exists in dbus-recbms >= 1.3.0).
