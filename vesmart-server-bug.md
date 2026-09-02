# vesmart-server crash loop on unhandled Bluetooth mgmt events

Report prepared for Victron. Observed on a Cerbo GX, 2026-09-02.

## Summary

`vesmart-server` dies with an unhandled `AttributeError` whenever the Bluetooth
management socket delivers an event code that `btmgmt_protocol.events` has no
`Packet` for. `IndexAddedEvent` (0x0004) and `IndexRemovedEvent` (0x0005) are
two such codes, and they are emitted whenever **any** controller on the system
appears or disappears — including one `vesmart-server` is not using, because the
mgmt socket is global rather than per-adapter.

On a GX with two controllers where one is unstable, this is a permanent crash
loop. Each crash powers the adapter down, which takes `dbus-ble-sensors` with
it, so every BLE tank and temperature sensor re-registers on D-Bus every few
minutes.

## Environment

| | |
|---|---|
| Venus OS | v3.75 (build 20260624163305) |
| `vesmart-server` | 0.5.14 (`vesmart_server.py:36`) |
| Controller in use | `hci1`, TP-Link UB500 USB, `2357:0604`, Realtek RTL8761B, HCI 5.1 |
| Second controller | `hci0`, built-in Broadcom BCM20702A1 |
| Service invocation | `vesmart_server.py -i hci1` |

## Symptom

```
File "/opt/victronenergy/vesmart-server/pin.py", line 154, in _btmgmt_callback_handler
  pkt: btmgmt_protocol.Response = btmgmt_protocol.reader(data)
File ".../ext/python-btsocket/btsocket/btmgmt_protocol.py", line 1055, in reader
  cmd_params = event_frame.decode(evt_params)
AttributeError: 'NoneType' object has no attribute 'decode'
INFO:pin:Adapter Powered off, canceling timers
INFO:vesmart_server:### Quiting program
*** starting vesmart-server ***
```

Four crashes in the log window sampled; the service was restarting roughly every
5–7 minutes indefinitely. `svstat` showed `vesmart-server` up 298 s and
`dbus-ble-sensors` up 299 s while every other service on the box had been up
12 days 19 h.

## Root cause

Two distinct defects.

**1. Unguarded lookup in `btmgmt_protocol.reader()`** (`btmgmt_protocol.py:1053`):

```python
event_frame = events.get(header.event_code.value)
cmd_params = event_frame.decode(evt_params)     # None.decode() when unknown
```

`events.get()` returns `None` for any code without a `Packet`, and the result is
dereferenced immediately. One unrecognised event kills the process.

**2. `events` table is missing codes the enum already defines.**

`class Events(Enum)` (line 511) defines `IndexAddedEvent = 0x0004`,
`IndexRemovedEvent = 0x0005`, `UnconfiguredIndexAddedEvent = 0x001d` and
`UnconfiguredIndexRemovedEvent = 0x001e`. The `events` dict (line 730) covers
`0x0001`–`0x001C` **except 0x0004 and 0x0005**, then `0x001F`–`0x0026`. So
`header.event_code` decodes successfully and the failure lands on the dict miss.

These four events carry no parameters, which is likely why they were skipped —
but they still need an empty `Packet([])` entry to be parseable. The enum also
stops at 0x0026, so kernel events ≥ 0x0027 (`ExperimentalFeatureChanged`,
`DeviceFlagsChanged`, `ControllerSuspend`/`Resume`, the `AdvertisementMonitor`
events) would fail earlier still, at the enum lookup.

## Why a second controller triggers it

`hci0` re-attaches continuously — kernel log, monotonic seconds:

```
[1108011] Bluetooth: hci0: BCM20702A1 (001.002.014) build 0000 ... MGMT ver 1.23
[1108418] Bluetooth: hci0: BCM: chip id 63 ... 'brcm/BCM20702A1.hcd' Patch ... MGMT ver 1.23
[1108858] Bluetooth: hci0: BCM: chip id 63 ... 'brcm/BCM20702A1.hcd' Patch ... MGMT ver 1.23
```

407 s and 440 s apart, matching the crash cadence. Every re-attach emits
`IndexRemovedEvent` then `IndexAddedEvent` on the shared mgmt socket, and
`vesmart-server` — bound to `hci1` — receives and chokes on them.

Also seen on the RTL8761B, probably unrelated to the crash but worth noting:

```
Bluetooth: hci1: Unexpected advertising set terminated event
Bluetooth: hci1: Opcode 0x2037 failed: -38      (-ENOSYS)
```

## Suggested fix

Make `reader()` skip what it cannot parse, and have the caller tolerate `None`:

```python
event_frame = events.get(header.event_code.value)
if event_frame is None:
    logger.debug('Ignoring unhandled mgmt event 0x%04x',
                 header.event_code.value)
    return None
```

```python
# pin.py:_btmgmt_callback_handler
pkt = btmgmt_protocol.reader(data)
if pkt is None:
    return True
```

Then add the missing zero-parameter entries to `events`:

```python
0x0004: Packet([]),   # IndexAddedEvent
0x0005: Packet([]),   # IndexRemovedEvent
0x001d: Packet([]),   # UnconfiguredIndexAddedEvent
0x001e: Packet([]),   # UnconfiguredIndexRemovedEvent
```

The guard matters more than the table: a management event added by a future
kernel should never be able to take the service down.

Separately, `vesmart-server` binds one adapter (`-i hci1`) but reacts to index
events for all of them. Filtering on `header.controller_idx` would be a
reasonable hardening on top.

## Workaround in place on this vessel

`Settings → Services → Bluetooth` off (`/Settings/Services/Bluetooth` = 0),
leaving `BleSensors` = 1. `vesmart-server` is torn down, `dbus-ble-sensors` stays
up, and the BLE tank and temperature sensors keep reporting. The cost is losing
the VictronConnect BLE interface.
