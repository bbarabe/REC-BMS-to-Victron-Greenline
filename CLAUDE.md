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

**Use one SSH session and keep it alive — always through `./cerbo`.** The Cerbo is
a small armv7l box with little SSH headroom: opening a connection per command — or
polling port 22 — exhausts it, and it then stops answering *entirely* for a good
while. `./cerbo` is the only sanctioned way in. It starts a local daemon
(`cerbo_daemon.py`) that holds **one** paramiko transport and runs every command as
a cheap extra channel on it; SFTP for `get`/`put` rides the same transport. Only
`./cerbo up` costs a handshake.

```sh
export CERBO_HOST=<cerbo-ip>      # not stored here: this repo is public
export CERBO_PASS=<root-password>

./cerbo up                                  # open the session (idempotent)
./cerbo 'uptime'                            # shorthand for `run`
./cerbo run 'svstat /service/dbus-recbms' 120   # optional timeout in seconds
./cerbo batch cmds.txt                      # one command per line, same session
./cerbo get /data/conf/settings.xml ./settings.xml
./cerbo put ./dbus-czone/config.ini /data/dbus-czone/config.ini
./cerbo status                              # alive? and the box's uptime
./cerbo down                                # close it
```

Rules that go with it:

- **Never open your own connection.** No bare `ssh`/`scp`, no per-command paramiko,
  no `ssh` in a loop. `cerbo_ssh.py` is the legacy one-shot runner and is kept only
  for the odd single read; prefer `./cerbo`.
- **Batch, don't chatter.** Put the whole task's commands in one `./cerbo batch`
  file rather than issuing them one tool call at a time.
- **A failed connect means stop and wait.** `./cerbo` enforces this: it stamps a
  backoff file and refuses to connect again for 10 minutes
  (`CERBO_BACKOFF_SECS`). Do not clear the stamp to "just try once more" — the
  retries are what cause the outage. If it says the transport is dead, the fix is
  `./cerbo down`, wait, then `./cerbo up`.
- **The session self-closes** after 30 min idle (`CERBO_IDLE_EXIT`), so a forgotten
  daemon never holds the box's one slot.
- **OpenSSH `ControlMaster` does not work here** — tried 2026-09-02: the master
  connects, then every multiplexed session is refused with
  `Master refused session request: Permission denied` and sshd stops answering.
  Use `./cerbo`.

Because a session can still be lost mid-task, make each step individually
verifiable: re-check `grep -m1 '^VERSION'` after a restart rather than assuming the
copy landed.

## Two deploy paths, do not mix them

| What | How | Who |
|---|---|---|
| `dbus-recbms/` (`dbus-recbms` + `dbus-solarpriority`), `dbus-czone/`, `dbus-batteries/`, `dbus-edrive/` | `scp` to `/data/`, then `svc -t` | scriptable |
| `*.json` Node-RED flows (all retired, `archive/` only) | import + Deploy in the Node-RED UI | **the user, by hand** |

No flow is deployed any more — everything is a driver, so the top row is the
only live path. The rule still stands for anything that goes back:
**never hot-deploy a flow** — do not splice `flows_einstein.json` on the Cerbo and
do not restart Signal K to force a flow reload. Finish flow work in the repo and
hand it over. Node-Red is *embedded in the Signal K server*, so restarting Node-Red
means restarting Signal K and everything else it hosts.

## Standalone drivers

Use the deploy script — it encodes every rule in this section (one session,
no connect retries, config value-guard, backups, restart only what changed,
verify by re-reading the shipped VERSION):

```sh
python deploy_cerbo.py recbms                  # upload changed files, svc -t, verify
python deploy_cerbo.py solarpriority --install # first install of a service
python deploy_cerbo.py recbms --dry-run        # show the plan / config diff only
python deploy_cerbo.py czone --verify-only     # no upload, no restart
```

Packages: `recbms`, `solarpriority`, `czone`, `batteries`, `edrive`.
`python test_drivers.py` runs `dbus-batteries` and `dbus-edrive` off the boat
against stubbed D-Bus, velib and SocketCAN — run it before every deploy of
either. `python test_solar_priority.py` does the same for dbus-recbms' sustain
control and the Solar Priority engine (one-way charge/discharge); the stand-ins
live in `test_stubs.py`. `solarpriority` reads `/RecBms/TargetSoc` and
`Sustain/*`, so deploy `recbms` first (publisher first, as always).

It aborts (exit 3) when the live config's *values* differ from the repo's
HEAD copy — fold the on-boat edit into the repo first, or `--force-config`.
Manual equivalent, if you must:

```sh
scp -r dbus-recbms root@$CERBO_HOST:/data/
ssh root@$CERBO_HOST 'svc -t /service/dbus-recbms'      # ~2s outage
```

`dbus-recbms/` holds two services: `dbus_recbms.py` (the BMS) and
`solar_priority.py` (`/service/dbus-solarpriority`, the Solar Priority engine).
One `scp -r` ships both; restart only the one you changed. The Solar Priority
driver and the `SolarPriority.json` flow must **never run together** (both
write `IgnoreAcIn1`).

`install.sh` is only for a **first** install (it creates `/service/<name>` and the
`/data/rc.local` hook that survives firmware updates). An already-installed driver
just needs the copy and the restart.

> **`scp -r` overwrites `config.ini`.** It holds the boat's calibration — the CVL
> curve, `solar_lead_v`, the solar-boost clamps, `momentary_outputs`, the e-drive
> DC-current fit and throttle divisor. Diff the live copy against the repo
> *before* copying, and fold any on-boat edits back into the repo first:
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

## Device instances

The Instance Registry flow is retired (`archive/InstanceRegistry.json`). Each
driver now pins the numbers it owns via
`/Settings/Devices/<id>/ClassAndVrmInstance`, using a settings id **without** the
`virtual_` prefix so the Node-RED palette's auto-cleanup can never remove it.
The allocation table is in `specification.md`, "Device Instance Allocation".

**Never reuse an instance**, even after deleting a device — VRM and Signal K key
their history off it, so a recycled number merges two devices' histories. New
`dbus-batteries` sources draw monotonically from
`/Settings/N2kBatteries/NextInstance`.

A driver replacing a flow must have the flow's `/Settings/Devices/virtual_*`
entries removed before it can claim the same numbers; each driver ships a
`migrate.sh` that does exactly that and refuses to run while the flow's services
are still on the bus.

**localsettings silently refuses a duplicate instance.** Two
`/Settings/Devices/*` entries may not claim one `ClassAndVrmInstance`: the
second `SetValue` returns success and keeps the old value. Always re-read after
pinning. And `RemoveSettings` wants the **leaf** path
(`Devices/<id>/ClassAndVrmInstance`) — given the group (`Devices/<id>`) it
returns `-1` per entry and changes nothing, silently. Both confirmed on the boat
2026-08-21.

**Disabling a flow tab + Deploy does not always drop its virtual services.**
They can survive as zombies holding their instances (seen with the CZone
migration). `svc -t /service/signalk-server` clears them — that is not a hot
deploy, it is cleanup *after* the user has deployed by hand.

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

## Deploy order when one component depends on another's D-Bus path

Publisher first, always. A consumer that reads a D-Bus path the running
publisher does not have yet will silently sit on missing data — e.g. the
retired `SolarPriority.json` needed `/RecBms/TargetChargeVoltage`, which only
exists in dbus-recbms >= 1.3.0.

## Runtime configuration lives in localsettings, not config.ini

`dbus-czone`'s per-output `Type` and everything under `dbus-batteries`'
`[sources]` are **first-run defaults**: once the setting exists in
localsettings, the user's (or an app's) choice wins and a redeploy must never
undo it. When adding a knob a UI should own, follow that pattern — seed from
`config.ini`, persist to `/Settings/<Group>/…`, and expose it on the driver's
own D-Bus service as well so a client can subscribe rather than poll.
`dbus-batteries` is the worked example: `com.victronenergy.n2kbatteries`
`/Sources/<key>/Enabled` and `/Settings/N2kBatteries/<key>/Enabled` are two
faces of one value.
