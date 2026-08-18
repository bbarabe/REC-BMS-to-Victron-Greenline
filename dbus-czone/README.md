# dbus-czone

Standalone Venus OS driver for the CZone switch bank. Replaces the Node-RED
"CZone Control" flow (`CZoneProxy.json`).

Runs as a daemontools service, comes up seconds after D-Bus at boot,
independent of Node-RED / Signal K, and is untouched by Node-RED deploys.

## What it publishes

One multi-output switch bank, the way Venus models the GX IO extender:

```
com.victronenergy.switch.czone          (device instance 224)
    /SwitchableOutput/output_1 .. output_N
        /Name              discovered from CZone
        /State             0/1, writable
        /Status            0/9
        /Settings/Type     0 = momentary, 1 = latching  (writable, persisted)
        /Settings/Group    "CZone Navigation" | "CZone Lighting" | "CZone Pump"
        /Settings/CustomName
```

The whole bank costs **one** device instance, instead of the eleven the
Node-RED flow consumed (225-235), and VRM renders it as a single unit.

## Auto-discovery

Nothing about the bank is hardcoded — not the circuit count, not the names,
not the bank instance. The wire format is documented in `specification.md`
("CZone circuit discovery").

1. **Passive, ~2 s.** Listen to PGN 127501. The bank instances the UC1
   broadcasts are the bank list, and the number of 2-bit fields that are
   not `3` ("unavailable") is the circuit count.
2. **Active, ~1 s.** For each circuit, send the CZone query PGN 65299 and
   read the name and category out of the PGN 130820 reply.

If the bus is silent (CZone powered down) the driver falls back to the table
it cached in localsettings at `/Settings/CZone/Circuits`.

Two traps worth knowing:

* The query's correlation tag **must have bit 7 clear**. With it set the UC1
  replies with a well-formed but *empty* record, which is indistinguishable
  from "no such circuit". The driver masks the tag itself.
* The Navico autopilot broadcasts PGN 127501 **instance 0** with every field
  zeroed at 5 Hz. That reads as a fully-configured 28-circuit bank, so any
  bank reporting full width is rejected.

## Control

Control uses PGN 127502 on the **bank-1** instance — the user latch that the
MFD and the keypads share — and mirrors what the MFD sends, byte for byte
(only the source address differs):

| Circuit type | `01` | `00` |
|---|---|---|
| **Latching** (lights) | button press — **toggles** the latch | release, ignored |
| **Momentary** (bilge pumps, horn) | on | off |

Because a latching press *toggles*, the driver only presses when a fresh
PGN 127501 reading disagrees with the target, and refuses the command
outright if state is stale (which can only mean the CAN feed has died — the
UC1 broadcasts every 2 s). A retry is issued only on positive evidence the
last send was lost: a status frame newer than it that still disagrees.

There is **no keep-alive and no echo guard**. CZone latches a bank-1 press
natively, and unlike the Node-RED flow this driver owns both sides of the
D-Bus service, so its own updates never re-enter as commands.

### The one thing CZone does not tell us

The latching/momentary split is absent from every message the UC1 sends, so it
lives in Venus's own per-output `Settings/Type`:
seeded from `momentary_outputs` in `config.ini`, then persisted. **Change it
in the Venus GUI** if you reconfigure a circuit in the CZone Configuration
Tool; the config file is only the first-run default.

## Install

```sh
scp -r dbus-czone root@<cerbo>:/data/
ssh root@<cerbo> /data/dbus-czone/install.sh
```

Order matters, same as the `dbus-recbms` migration:

1. **Disable the Node-RED CZone flow first** (or delete the tab and deploy).
   Leaving it running means two writers on the same circuits.
2. **Clean up its registry entries**, or the driver cannot pin its instance:
   `RemoveSettings` must be called on the `/Settings` object with keys
   **relative** to it — full paths, or calling on `/Settings/Devices`,
   return `-1` for every key:
   ```sh
   ARR=$(for i in $(seq 1 11); do \
     printf '"Devices/virtual_cz_vs_sw%d/ClassAndVrmInstance",' $i; done | sed 's/,$//')
   dbus -y com.victronenergy.settings /Settings RemoveSettings "%[$ARR]"
   # expect [0, 0, ...]; -1 means the call form is wrong, not that the key was absent
   dbus -y | grep virtual_cz_vs        # must be empty; if not, restart signalk-server
   ```
   Deleting a Node-RED flow can leave a zombie D-Bus service behind — the
   palette's name-release on node close is unreliable.
3. `install.sh`.

Check it came up:

```sh
svstat /service/dbus-czone
tail -f /var/log/dbus-czone/current | tai64nlocal
dbus -y com.victronenergy.switch.czone / GetValue
```

The log names every circuit it discovered, so a successful start is obvious.

## Uninstall

```sh
/data/dbus-czone/uninstall.sh
```

Stops the service and removes the autostart hook; leaves `/data/dbus-czone`,
instance 224 and the cached circuit table in place.

## Notes

- `velib_python` is imported from the copy shipped with Venus OS
  (`/opt/victronenergy/dbus-systemcalc-py/ext/velib_python`) so the API
  always matches the running stack.
- The settings id is deliberately **not** prefixed `virtual_`: the Victron
  Node-RED palette auto-deletes `/Settings/Devices/virtual_*` entries that
  have no live service.
- `socket.CAN_EFF_FLAG` is a **negative** int on armv7l — it is masked to u32
  before `struct.pack`, or the pack raises.
- Only bank 1 is driven. A CZone configuration may define further banks over
  the same outputs, but the outputs OR together, so a secondary bank cannot
  clear a latch set on bank 1 — it can only add. Bank 1 is the user latch the
  MFD and keypads share.
