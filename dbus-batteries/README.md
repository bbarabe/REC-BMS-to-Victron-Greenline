# dbus-batteries

Standalone Venus OS driver for the NMEA 2000 12 V batteries. Replaces the
Node-RED "Batteries Forward" flow (`archive/BatteriesForward.json`).

Runs as a daemontools service, comes up seconds after D-Bus at boot,
independent of Node-RED / Signal K, and is untouched by Node-RED deploys.

## Where the data comes from

The flow subscribed to Signal K paths, so the batteries only appeared once the
whole Signal K stack was up and its N2K decoder had converged. This driver
reads the same three PGNs straight off `can0` with kernel filters:

| PGN | Frame | Fields used | Sender on this boat |
|---|---|---|---|
| 127508 Battery Status | single | instance, voltage (0.01 V), current (0.1 A), temperature (0.01 K) | SIMARINE SN01 SiCOM, N2K addr `0x10` |
| 127506 DC Detailed Status | fast packet | instance, DC type, SOC %, SOH %, time remaining | same |
| 127489 Engine Parameters, Dynamic | fast packet | engine instance, alternator potential (0.01 V) | Yanmar gateways, src `0x8D` / `0x8E` |

Both decodes are verified against `captures/mfd-boot.log`:

```
15F21410 # 01 8C 05 03 00 FF FF FF        -> instance 1, 14.20 V, +0.3 A
15F21410 # 02 64 05 FF 7F FF FF FF        -> instance 2, 13.80 V, current N/A
19F21210 # (reassembled) 00 01 00 64 64 …  -> instance 1, battery, SOC 100 %, SOH 100 %
```

The engine batteries are **not** on the SiCOM. Their voltage is the alternator
potential the Yanmar gateways broadcast at ~2 Hz; 127489 carries no current and
no SOC, so those paths are not created at all for them (the flow's
`default_values: false`, done properly).

The gateways are powered from the **ignition**, not the engine: with the
ignition off there is no 127489 on the bus at all, and the engine batteries are
simply absent (see the staleness table below). Ignition on with the engine not
running is enough to see them.

Reserved N2K codes are honoured: `0xFFFF` / `0x7FFF` mean "not available" and
produce `None`, not `655.35 V`.

## What it publishes

One battery service per forwarded source **that is actually being received**:

```
com.victronenergy.battery.n2kbat_house     (instance 201)  bat1  House
com.victronenergy.battery.n2kbat_bow       (instance 202)  bat2  Bow
com.victronenergy.battery.n2kbat_stern     (instance 203)  bat3  Stern
com.victronenergy.battery.n2kbat_porteng   (instance 204)  alt0  Port Engine
com.victronenergy.battery.n2kbat_stbdeng   (instance 205)  alt1  Starboard Engine
```

Instances carried over unchanged from `InstanceRegistry.json`, so VRM history
and Signal K paths survive the migration.

Paths on a **DC** source (127508 + 127506):

```
/Dc/0/Voltage  /Dc/0/Current  /Dc/0/Power  /Dc/0/Temperature
/Soc  /Soh  /TimeToGo
/InstalledCapacity  /Capacity  /ConsumedAmphours     (when a capacity is set)
/Connected  /CustomName (writable, persisted)
/N2k/SourceKey  /N2k/Instance  /N2k/SourceAddress
```

An **alternator** source (127489) gets `/Dc/0/Voltage` and the housekeeping
paths only.

`/Capacity` and `/ConsumedAmphours` derive from SOC × `InstalledCapacity`, with
consumed negative (BMV convention), and only exist while a capacity is
configured.

**Staleness** happens in two stages:

| after | what happens |
|---|---|
| `stale_after_s` (10 s) | `/Connected = 0` and every measurement **blanked**. Cleared rather than frozen, because a stale 12.6 V on a start battery reads exactly like a healthy one. |
| `unpublish_after_s` (300 s) | the battery leaves D-Bus **entirely** — no service, nothing in VRM. |

A source that has **never** been seen is never published at all, whatever
`unpublish_after_s` says.

That second stage is why the engine batteries are usually absent: the Yanmar
gateways are unpowered with the ignition off, so PGN 127489 simply is not on the
bus, and a permanently disconnected device in VRM is worse than no device. The
source keeps its settings, its name, its capacity and its **reserved instance**,
and comes back on the same number one frame after the ignition goes on. Set
`unpublish_after_s = 0` to publish every enabled source permanently.

`Enabled` is the standing choice ("forward this when it is there"); `Published`
on the manager service is the bus's answer ("it is there right now"). The
catalog lists both, so a UI can show a battery that is configured but currently
absent.

## Choosing which batteries are forwarded

This is the part the flow could not do: which batteries exist and which of them
reach VRM is data, not code.

Every source the bus mentions is catalogued under a key derived from the wire —
`bat<n>` for N2K battery/DC instance *n*, `alt<n>` for engine instance *n* —
never from a list position. A source that is catalogued but not enabled costs
nothing: no device instance, no service, no VRM entry.

Two equivalent control surfaces, both persisting to the same localsettings
values, both applied **immediately** (the battery service is created or torn
down in place — no restart):

**1. localsettings** — reachable from anything that can see
`com.victronenergy.settings`, including the Venus MQTT bridge:

```
/Settings/N2kBatteries/<key>/Enabled          0/1
/Settings/N2kBatteries/<key>/Instance         VRM device instance
/Settings/N2kBatteries/<key>/CustomName
/Settings/N2kBatteries/<key>/ServiceSuffix    dbus name + settings id
/Settings/N2kBatteries/<key>/Capacity         Ah, 0 = unknown
/Settings/N2kBatteries/Catalog                JSON, every known source
/Settings/N2kBatteries/NextInstance           allocator cursor
```

**2. a management service** — structured, subscribable, for a UI:

```
com.victronenergy.n2kbatteries
    /SourceCount  /EnabledCount  /SettingsGroup
    /Catalog                       JSON, same content as the setting
    /Sources/<key>/Enabled         0/1        writable
    /Sources/<key>/DeviceInstance             writable
    /Sources/<key>/CustomName                 writable
    /Sources/<key>/Capacity                   writable
    /Sources/<key>/Kind            "dc" | "alternator"
    /Sources/<key>/N2kInstance
    /Sources/<key>/SourceAddress
    /Sources/<key>/Available       0/1, is it on the bus right now
    /Sources/<key>/Published       0/1, does it currently exist as a device
    /Sources/<key>/Age             seconds since its last frame
    /Sources/<key>/Fields          e.g. "current,soc,soh,voltage"
```

`/Catalog` is a JSON array — one object per source with `key`, `kind`, `n2k`,
`fields`, `enabled`, `instance`, `name`, `suffix` and `capacity` — so a client
can render the whole picker from a single read. Source address and published
state are on `/Sources/<key>/SourceAddress` and `/Published` only: two senders
sharing an instance flip the address on every frame, and while it was in the
blob that meant a localsettings write and a settings reconcile most seconds.

```json
[{"key":"bat1","kind":"dc","n2k":1,"fields":["current","soc","soh","voltage"],
  "enabled":1,"instance":201,"name":"House","suffix":"house","capacity":130},
 {"key":"alt0","kind":"alternator","n2k":0,"fields":[],
  "enabled":1,"instance":204,"name":"Port Engine","suffix":"porteng",
  "capacity":0}]
```

That second entry is the live state at the dock: configured, instance reserved,
never yet seen.

Only the *stable* description is persisted; liveness lives on `/Available` and
`/Age`, so a battery that is present does not cause a localsettings write every
second. `/Age` itself is refreshed every `age_refresh_s` (10 s), not every tick.
Every service publishes a tick's changes as one `ItemsChanged`, with values
quantised to the `[publish]` steps, so a steady battery costs no bus traffic.

Writes to the settings made by something else (an app talking to the MQTT
bridge, or `dbus -y`) are picked up from the `PropertiesChanged` signal, with a
`reconcile_s` (30 s) poll as the backstop.

### config.ini is only a first-run default

Everything under `[sources]` seeds localsettings the first time a key is seen
and is ignored afterwards — the same contract as `dbus-czone`'s per-output
`Type`. A redeploy can never undo a choice made in the GUI. To force a value
back, delete the setting and restart:

`RemoveSettings` takes **leaf** paths, one per setting — handed a group
(`N2kBatteries/bat2`) it returns `-1` for every entry and changes nothing,
without saying so:

```sh
dbus -y com.victronenergy.settings /Settings RemoveSettings   '%["N2kBatteries/bat2/Enabled","N2kBatteries/bat2/Instance",
     "N2kBatteries/bat2/CustomName","N2kBatteries/bat2/ServiceSuffix",
     "N2kBatteries/bat2/Capacity"]'
svc -t /service/dbus-batteries
```

### New batteries

A source that appears on the bus and is not in `config.ini` is catalogued and
left **disabled** (`auto_enable_new = false`), so the boat never sprouts VRM
devices by itself. Enable it and it gets the next number from
`instance_pool` (206-209).

Instances are allocated **monotonically and never reused**, because VRM and
Signal K key their history off the instance: recycling a number silently merges
two different batteries' histories. That is the rule `InstanceRegistry.json`
existed to enforce, now enforced by the driver that owns the numbers.

## Install

First install only:

```sh
scp -r dbus-batteries root@$CERBO_HOST:/data/
ssh root@$CERBO_HOST '/data/dbus-batteries/install.sh'
```

or `python deploy_cerbo.py batteries --install` from the repo, which encodes
the one-session / config-guard rules in `CLAUDE.md`.

Afterwards a plain `python deploy_cerbo.py batteries` uploads what changed and
restarts the service.

## Migration from the flow

The flow's five virtual batteries hold instances 201-205 in
`/Settings/Devices/virtual_bat<N>_virtual/ClassAndVrmInstance`. While those
entries exist localsettings will not give this driver the same numbers, and the
batteries reappear in VRM as new devices with no history.

1. Node-RED UI: disable the **Batteries Forward** tab, Deploy.
   (Flows are deployed by hand — see `CLAUDE.md`.)
2. `ssh root@$CERBO_HOST '/data/dbus-batteries/migrate.sh'` — refuses to run
   while a flow service is still on the bus, prints what it removes.
3. `svc -t /service/dbus-batteries`.

Rollback: `uninstall.sh`, then re-import `archive/BatteriesForward.json` and
`archive/InstanceRegistry.json`. The flow and this driver must **never** run
together — they would both claim 201-205.

## Verify

```sh
svstat /service/dbus-batteries
grep -m1 '^VERSION' /data/dbus-batteries/dbus_batteries.py
tail -f /var/log/dbus-batteries/current | tai64nlocal
dbus -y com.victronenergy.battery.n2kbat_house / GetValue
dbus -y com.victronenergy.n2kbatteries /Catalog GetValue
```

A healthy start looks like:

```
dbus-batteries v1.0.0 starting (velib: /opt/victronenergy/…/velib_python)
listening on can0 for PGN 127508, 127506 and 127489
catalog: 5 source(s) restored from settings
bat1: forwarding as com.victronenergy.battery.n2kbat_house (instance 201, 'House')
…
```

## Notes

- **No alarms.** The driver publishes measurements only. A low-voltage alarm on
  a start battery that is simply not being charged would fire every night at
  anchor; the REC-BMS driver owns the alarm story for the pack that matters.
- These services are ordinary battery monitors, so they are selectable as the
  system battery service. Leave `/Settings/SystemSetup/BatteryService` where it
  is — `dbus-recbms` (instance 200) is the pack DVCC must follow, and it pins
  `/Settings/SystemSetup/BmsInstance` itself.
- The driver never transmits on `can0`.
- **A device instance is reserved for every enabled source, published or not.**
  localsettings will not let two `/Settings/Devices/*` entries claim one
  instance — and it refuses the write *silently*, reporting success while
  keeping the old value (confirmed on the boat 2026-08-21). So an unpinned
  source could have its number handed to the next device that asks and come
  back with its VRM history split. The driver pins at startup and re-reads to
  confirm the write took; if something else holds the number it says so and
  names `migrate.sh` as the fix, rather than publishing an instance
  localsettings does not agree it owns.
