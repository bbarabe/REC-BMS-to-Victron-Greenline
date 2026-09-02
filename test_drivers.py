#!/usr/bin/env python3
"""
test_drivers.py — run dbus-batteries and dbus-edrive off the boat.

    python test_drivers.py

Stubs dbus, velib_python, GLib and the SocketCAN socket, then constructs both
drivers for real and feeds them frames. The fake CAN socket ENFORCES each
driver's own CAN_RAW_FILTER, so a frame the kernel would drop cannot pass here
either — a filter bug fails the test rather than hiding until deploy.

The 12 V battery frames are lifted verbatim from captures/mfd-boot.log; the
e-drive frames are built from the decode in GreenlineFindings.md (the captures
in this repo were taken with a CZone-only filter and contain no drive traffic).

Covers: construction, CAN filters, instance pinning, per-source settings, the
publish path and its rounding, staleness blanking, enable/disable from both the
manager service and localsettings, renaming, and discovery of a battery that is
not in config.ini. Exit status is 0 only if everything passes.
"""
from test_stubs import *   # noqa: F401,F403 -- dbus/velib/GLib/CAN stand-ins


# ============================================================ dbus-batteries
print("\n=== dbus-batteries ===")
B = load(os.path.join(REPO, "dbus-batteries", "dbus_batteries.py"), "dbus_batteries")
cfg = B.Config(os.path.join(REPO, "dbus-batteries", "config.ini"))
check("config: 5 sources seeded", cfg.enabled_default ==
      ["bat1", "bat2", "bat3", "alt0", "alt1"], str(cfg.enabled_default))
check("config: instance map", cfg.instances ==
      {"bat1": 201, "bat2": 202, "bat3": 203, "alt0": 204, "alt1": 205})
check("config: names keep spaces", cfg.names["alt1"] == "Starboard Engine",
      repr(cfg.names.get("alt1")))

drv = B.BatteryDriver(cfg)
sock = drv._sock
ents = filter_entries(drv._sock.filters)
check("CAN: 3 filter entries, all EFF", len(ents) == 3 and
      all(e[0] & 0x80000000 for e in ents), str([hex(e[0]) for e in ents]))

svcs = {n: s for n, s in FakeBus.names.items() if n.startswith("com.victronenergy.battery")}
check("only sources that have been SEEN are published (none yet)",
      len(svcs) == 0, str(sorted(svcs)))
mgr0 = FakeBus.names["com.victronenergy.n2kbatteries"]
check("manager service present", "com.victronenergy.n2kbatteries" in FakeBus.names)
check("catalogued but unpublished sources still listed for the app",
      mgr0.values["/SourceCount"] == 5 and mgr0.values["/EnabledCount"] == 5,
      "%s/%s" % (mgr0.values["/SourceCount"], mgr0.values["/EnabledCount"]))

# feed the real capture frames
# all three verbatim from captures/mfd-boot.log
sock.feed(0x15F21410, bytes.fromhex("018C050300FFFFFF"))   # inst 1, 14.20 V, 0.3 A
sock.feed(0x15F21410, bytes.fromhex("0264050000FFFFFF"))   # inst 2, 13.80 V
sock.feed(0x15F21410, bytes.fromhex("035A050000FFFFFF"))   # inst 3, 13.70 V
sock.feed(0x19F21210, bytes.fromhex("2009000100646400"))
sock.feed(0x19F21210, bytes.fromhex("2100000000FFFFFF"))
sock.feed(0x19F21210, bytes.fromhex("4009000200646400"))
sock.feed(0x19F21210, bytes.fromhex("4100000000FFFFFF"))
sock.feed(0x19F21210, bytes.fromhex("6009000300646400"))
sock.feed(0x19F21210, bytes.fromhex("6100000000FFFFFF"))
# engine 0 alternator = 13.42 V (0x053E), inside a 26-byte fast packet
eng = bytearray(26)
eng[0] = 0
struct.pack_into("<h", eng, 7, 1342)
sock.feed(0x19F2018D, bytes([0x00, 26]) + bytes(eng[:6]))
for i in range(1, 5):
    sock.feed(0x19F2018D, bytes([i]) + bytes(eng[6 + (i - 1) * 7: 6 + i * 7]))
drv._can_readable(0, GLib.IO_IN)
drv._tick()

svcs = {n: s for n, s in FakeBus.names.items()
        if n.startswith("com.victronenergy.battery")}
check("a source appearing on the bus publishes itself", len(svcs) == 4,
      str(sorted(svcs)))
house = FakeBus.names["com.victronenergy.battery.n2kbat_house"]
porteng = FakeBus.names["com.victronenergy.battery.n2kbat_porteng"]
check("instances 201-204 for the four that spoke",
      sorted(x.values["/DeviceInstance"] for x in svcs.values()) == [201, 202, 203, 204],
      str(sorted(x.values["/DeviceInstance"] for x in svcs.values())))
check("silent starboard engine is NOT on dbus",
      "com.victronenergy.battery.n2kbat_stbdeng" not in FakeBus.names)
check("house has /Soc", "/Soc" in house.values)
check("engine battery has NO /Soc and NO /Dc/0/Current",
      "/Soc" not in porteng.values and "/Dc/0/Current" not in porteng.values)
check("house voltage 14.20", house.values["/Dc/0/Voltage"] == 14.2,
      str(house.values["/Dc/0/Voltage"]))
check("house current 0.3", house.values["/Dc/0/Current"] == 0.3)
check("house power 4.3 (1 dp, as the flow did)", house.values["/Dc/0/Power"] == 4.3,
      str(house.values["/Dc/0/Power"]))
check("house SOC 100", house.values["/Soc"] == 100)
check("house capacity 130Ah -> /Capacity 130.0, consumed -0.0",
      house.values["/Capacity"] == 130.0 and house.values["/ConsumedAmphours"] == 0.0,
      "%s %s" % (house.values["/Capacity"], house.values["/ConsumedAmphours"]))
check("house connected", house.values["/Connected"] == 1)
check("port engine 13.42 V", porteng.values["/Dc/0/Voltage"] == 13.42,
      str(porteng.values["/Dc/0/Voltage"]))
check("stbd engine absent from dbus, still in the catalog",
      "com.victronenergy.battery.n2kbat_stbdeng" not in FakeBus.names and
      "alt1" in [c["key"] for c in json.loads(mgr0.values["/Catalog"])])

mgr = FakeBus.names["com.victronenergy.n2kbatteries"]
cat = json.loads(mgr.values["/Catalog"])
check("catalog lists 5 sources", len(cat) == 5, str([c["key"] for c in cat]))
check("catalog has no volatile field", all("age" not in c for c in cat))
bycat = {c["key"]: c for c in cat}
check("bat1 fields discovered",
      set(bycat["bat1"]["fields"]) >= {"voltage", "current", "soc", "soh"},
      str(bycat["bat1"]["fields"]))
check("alt0 fields = voltage only", bycat["alt0"]["fields"] == ["voltage"],
      str(bycat["alt0"]["fields"]))
check("bat1 available", mgr.values["/Sources/bat1/Available"] == 1)
check("EnabledCount 5", mgr.values["/EnabledCount"] == 5)
check("catalog persisted to settings",
      FakeBus.store.get("/Settings/N2kBatteries/Catalog") == mgr.values["/Catalog"])

# staleness
for s in drv.sources.values():
    s.last_seen -= 20
drv._tick()
check("stale -> disconnected + blanked",
      house.values["/Connected"] == 0 and house.values["/Dc/0/Voltage"] is None)
check("stale -> manager Available 0", mgr.values["/Sources/bat1/Available"] == 0)
check("stale forgets the measurements, keeps the identity",
      drv.sources["bat1"].voltage is None and drv.sources["bat1"].soc is None
      and drv.sources["bat1"].fields >= {"voltage", "soc"})
# a source that comes back with only 127508 must not republish the old SOC
sock.feed(0x15F21410, bytes.fromhex("018C050300FFFFFF"))
drv._can_readable(0, GLib.IO_IN)
drv._tick()
check("partial recovery: voltage back, SOC still blank",
      house.values["/Dc/0/Voltage"] == 14.2 and house.values["/Soc"] is None,
      "%s / %s" % (house.values["/Dc/0/Voltage"], house.values["/Soc"]))

# disable bat2 through the manager service
mgr.write("/Sources/bat2/Enabled", 0)
check("disable removes the service",
      "com.victronenergy.battery.n2kbat_bow" not in FakeBus.names)
check("disable persisted", FakeBus.store["/Settings/N2kBatteries/bat2/Enabled"] == 0)
check("EnabledCount 4", mgr.values["/EnabledCount"] == 4)
# re-enable
mgr.write("/Sources/bat2/Enabled", 1)
check("re-enable brings it back at 202",
      FakeBus.names["com.victronenergy.battery.n2kbat_bow"].values["/DeviceInstance"] == 202)

# disable through localsettings instead (what the MQTT bridge would do)
FakeBus.store["/Settings/N2kBatteries/bat3/Enabled"] = 0
drv._reconcile()
check("settings write honoured by reconcile",
      "com.victronenergy.battery.n2kbat_stern" not in FakeBus.names)
FakeBus.store["/Settings/N2kBatteries/bat3/Enabled"] = 1
drv._reconcile()
check("and back", "com.victronenergy.battery.n2kbat_stern" in FakeBus.names)

# capacity is configuration, not a measurement: a change lands even while the
# battery is offline (bat3 is stale at this point)
FakeBus.store["/Settings/N2kBatteries/bat3/Capacity"] = 200
drv._reconcile()
stern = FakeBus.names["com.victronenergy.battery.n2kbat_stern"]
check("capacity change lands while disconnected",
      stern.values["/InstalledCapacity"] == 200 and stern.values["/Connected"] == 0,
      str(stern.values["/InstalledCapacity"]))

# every ENABLED source reserves its instance in localsettings, published or
# not — an unpublished battery that lost its pin would come back on a different
# instance and split its VRM history
check("silent alt1 still reserves instance 205",
      str(FakeBus.store.get(
          "/Settings/Devices/n2kbat_stbdeng/ClassAndVrmInstance")) == "battery:205",
      str(FakeBus.store.get("/Settings/Devices/n2kbat_stbdeng/ClassAndVrmInstance")))
check("...even though it is not on D-Bus",
      "com.victronenergy.battery.n2kbat_stbdeng" not in FakeBus.names)

# a squatter on the wanted number must be reported, not silently accepted
FakeBus.store["/Settings/Devices/squatter/ClassAndVrmInstance"] = "battery:212"
FakeBus.store["/Settings/N2kBatteries/bat3/Instance"] = 212
drv._reconcile()
check("a refused pin is detected, not published as if it stuck",
      drv.sources["bat3"].instance != 212 and
      FakeBus.store["/Settings/Devices/squatter/ClassAndVrmInstance"] == "battery:212",
      "bat3 instance = %s" % drv.sources["bat3"].instance)
del FakeBus.store["/Settings/Devices/squatter/ClassAndVrmInstance"]
FakeBus.store["/Settings/N2kBatteries/bat3/Instance"] = 203
drv._reconcile()
check("and bat3 recovers 203 once the squatter is gone",
      drv.sources["bat3"].instance == 203, str(drv.sources["bat3"].instance))

# a battery that goes quiet for unpublish_after_s leaves D-Bus entirely,
# instead of sitting in VRM as a permanently disconnected device
for src in drv.sources.values():
    src.last_seen -= cfg.unpublish_after_s + 5
drv._tick()
check("long silence removes the device from D-Bus",
      "com.victronenergy.battery.n2kbat_house" not in FakeBus.names)
check("...but it keeps its catalog entry, instance and Enabled",
      drv.sources["bat1"].enabled and drv.sources["bat1"].instance == 201 and
      "bat1" in [c["key"] for c in json.loads(mgr.values["/Catalog"])])
check("manager reports it unpublished but still enabled",
      mgr.values["/Sources/bat1/Published"] == 0 and
      mgr.values["/Sources/bat1/Enabled"] == 1)
sock.feed(0x15F21410, bytes.fromhex("018C050300FFFFFF"))
drv._can_readable(0, GLib.IO_IN)
drv._tick()
house = FakeBus.names["com.victronenergy.battery.n2kbat_house"]
check("one frame brings it back on the same instance",
      house.values["/DeviceInstance"] == 201 and house.values["/Connected"] == 1)
check("manager reports it published again",
      mgr.values["/Sources/bat1/Published"] == 1)

# rename through the battery service
house.write("/CustomName", "House Bank")
check("rename persisted",
      FakeBus.store["/Settings/N2kBatteries/bat1/CustomName"] == "House Bank")

# a battery nobody configured turns up
sock.feed(0x15F21410, bytes.fromhex("079C050000FFFFFF"))
drv._can_readable(0, GLib.IO_IN)
drv._tick()
check("unknown source catalogued", "bat7" in drv.sources)
check("unknown source NOT forwarded", drv.sources["bat7"].enabled is False)
check("unknown source got a pool instance",
      drv.sources["bat7"].instance == 206, str(drv.sources["bat7"].instance))
check("unknown source visible to the app",
      "bat7" in [c["key"] for c in json.loads(mgr.values["/Catalog"])])
mgr.write("/Sources/bat7/Enabled", 1)
check("enabling the new one publishes it at 206",
      FakeBus.names["com.victronenergy.battery.n2kbat_bat7"].values["/DeviceInstance"] == 206)

# ================================================================ dbus-edrive
print("\n=== dbus-edrive ===")
E = load(os.path.join(REPO, "dbus-edrive", "dbus_edrive.py"), "dbus_edrive")
ecfg = E.Config(os.path.join(REPO, "dbus-edrive", "config.ini"))
check("config: 2 drives", len(ecfg.drives) == 2)
p, s = ecfg.drives[0], ecfg.drives[1]
check("port: node 0x0A / HCU 0x65 / n2k 0 / inst 210",
      (p.canopen_node, p.hcu_source, p.n2k_instance, p.instance) == (0x0A, 0x65, 0, 210))
check("stbd: node 0x0B / HCU 0x64 / n2k 1 / inst 211",
      (s.canopen_node, s.hcu_source, s.n2k_instance, s.instance) == (0x0B, 0x64, 1, 211))

edrv = E.EDriveDriver(ecfg)
esock = edrv._sock
ents = filter_entries(esock.filters)
sff = [e for e in ents if not (e[0] & 0x80000000)]
eff = [e for e in ents if e[0] & 0x80000000]
check("CAN: 8 SFF + 5 EFF filter entries", len(sff) == 8 and len(eff) == 5,
      "%d/%d" % (len(sff), len(eff)))
check("SFF entries force 11-bit", all(e[1] & 0x80000000 for e in sff))
check("SFF ids are 0x18A..0x48B",
      sorted(hex(e[0]) for e in sff) ==
      sorted(hex(c | n) for c in E.TPDO_CODES for n in (0x0A, 0x0B)))

port = FakeBus.names["com.victronenergy.motordrive.edrive_port"]
stbd = FakeBus.names["com.victronenergy.motordrive.edrive_stbd"]
check("two motordrive services", port is not None and stbd is not None)
check("instances 210/211", (port.values["/DeviceInstance"],
                            stbd.values["/DeviceInstance"]) == (210, 211))
check("nine motordrive paths present",
      all(x in port.values for x in
          ("/Dc/0/Voltage", "/Dc/0/Current", "/Dc/0/Power", "/Motor/RPM",
           "/Motor/Direction", "/Motor/Temperature", "/Controller/Temperature",
           "/Coolant/Temperature", "/Connected")))
check("/EDrive telemetry present", "/EDrive/TorqueNm" in port.values)

# --- feed a realistic port-drive burst
esock.feed(0x18A, bytes([1]), extended=False)                        # running
esock.feed(0x28A, struct.pack("<ff", 18.72, 13.50), extended=False)  # mosfet, drive
esock.feed(0x38A, struct.pack("<ff", 52.30, 190.0), extended=False)  # V, Irms
esock.feed(0x48A, struct.pack("<ff", 45.6, 422.0), extended=False)   # torque, rpm
esock.feed(0x14F00B65, bytes([0, 153, 0, 0, 0, 0, 0, 0]))            # torque 28 %
esock.feed(0x14F00C65, struct.pack("<HHHH", 0, 30000, 5595, 0))      # DC A, peak A
def enc(c): return int(round((c + 273) / 0.03125))
esock.feed(0x14F00D65, struct.pack("<HHHH", enc(18.7), enc(13.5), enc(41.0), 0))
esock.feed(0x18FF5365, bytes([0, 75, 0, 0, 0, 0, 0, 0]))             # throttle
esock.feed(0x09F2058D, bytes([0, 0xFC, 0, 0, 0, 0, 0, 0]))           # forward
# and one starboard frame to prove the inverted HCU mapping
esock.feed(0x14F00D64, struct.pack("<HHHH", enc(19.0), enc(12.9), enc(38.0), 0))
edrv._can_readable(0, GLib.IO_IN)
edrv._tick()

check("port connected", port.values["/Connected"] == 1)
check("port rpm 422", port.values["/Motor/RPM"] == 422)
check("port voltage 52.3", port.values["/Dc/0/Voltage"] == 52.3)
check("port DC current 52.87 A (61452 preferred)",
      port.values["/Dc/0/Current"] == 52.87, str(port.values["/Dc/0/Current"]))
check("port power = V x I", port.values["/Dc/0/Power"] == round(52.3 * 52.87),
      str(port.values["/Dc/0/Power"]))
check("Motor/Temperature = drive temp 13.5 (0x28x f2)",
      port.values["/Motor/Temperature"] == 13.5, str(port.values["/Motor/Temperature"]))
check("Coolant/Temperature = MOSFET 18.7 (0x28x f1 preferred over 61453 w0)",
      port.values["/Coolant/Temperature"] == 18.7,
      str(port.values["/Coolant/Temperature"]))
check("Controller/Temperature = MCU 41.0 (61453 w2 only)",
      port.values["/Controller/Temperature"] == 41.0,
      str(port.values["/Controller/Temperature"]))
check("direction forward", port.values["/Motor/Direction"] == E.DIR_FORWARD)
check("torque 45.6 Nm", port.values["/EDrive/TorqueNm"] == 45.6)
check("torque 28 %", port.values["/EDrive/TorquePercent"] == 28)
check("throttle 8.08 %", port.values["/EDrive/ThrottlePercent"] == 8.08,
      str(port.values["/EDrive/ThrottlePercent"]))
check("phase peak 279.8 A", port.values["/EDrive/PhaseCurrentPeak"] == 279.8,
      str(port.values["/EDrive/PhaseCurrentPeak"]))
check("mech power T*w", port.values["/EDrive/MechanicalPower"] ==
      int(round(45.6 * 422.0 * 3.141592653589793 / 30)),
      str(port.values["/EDrive/MechanicalPower"]))
check("running", port.values["/EDrive/Running"] == 1)
check("HCU 0x64 landed on STARBOARD, not port",
      stbd.values["/Controller/Temperature"] == 38.0 and
      port.values["/Controller/Temperature"] == 41.0,
      "stbd=%s port=%s" % (stbd.values["/Controller/Temperature"],
                           port.values["/Controller/Temperature"]))
check("starboard has no CANopen data yet",
      stbd.values["/Motor/RPM"] is None and stbd.values["/Dc/0/Voltage"] is None)

# rpm deadband + gear "unavailable"
esock.feed(0x48A, struct.pack("<ff", 0.0, -0.4), extended=False)
esock.feed(0x09F2058D, bytes([0, 0xFF, 0, 0, 0, 0, 0, 0]))   # gear 3 = unavailable
edrv._can_readable(0, GLib.IO_IN)
edrv._tick()
check("rpm deadband clamps -0.4 to 0", port.values["/Motor/RPM"] == 0)
check("gear 'unavailable' leaves direction alone",
      port.values["/Motor/Direction"] == E.DIR_FORWARD)

# staleness per drive
for d in edrv.drives:
    if d.key == "port":
        d.last_seen -= 60
edrv._tick()
check("port stale -> blanked", port.values["/Connected"] == 0 and
      port.values["/Motor/RPM"] is None and port.values["/EDrive/TorqueNm"] is None)
check("starboard unaffected by port staleness", stbd.values["/Connected"] == 1)
check("stale drive forgets its HCU state", edrv.drives[0].mcu_temp is None)
# the Yanmar gateway keeps broadcasting gear/throttle with the drives powered
# down: those frames must NOT resurrect /Connected
esock.feed(0x09F2058D, bytes([0, 0xFC, 0, 0, 0, 0, 0, 0]))
esock.feed(0x18FF5365, bytes([0, 75, 0, 0, 0, 0, 0, 0]))
edrv._can_readable(0, GLib.IO_IN)
edrv._tick()
check("gateway gear/throttle do not fake a live drive",
      port.values["/Connected"] == 0)
# the drives come back one frame family at a time: CANopen first, no 61453 yet
esock.feed(0x38A, struct.pack("<ff", 51.0, 10.0), extended=False)
edrv._can_readable(0, GLib.IO_IN)
edrv._tick()
check("partial recovery: voltage back, MCU temp still blank",
      port.values["/Dc/0/Voltage"] == 51.0 and
      port.values["/Controller/Temperature"] is None,
      "%s / %s" % (port.values["/Dc/0/Voltage"],
                   port.values["/Controller/Temperature"]))

# rename
port.write("/CustomName", "Port Drive")
check("edrive rename persisted",
      FakeBus.store["/Settings/EDrive/port/CustomName"] == "Port Drive")

print("\n%d passed, %d failed" % (len(ok), len(fail)))
for f in fail:
    print("  FAILED: " + f)
sys.exit(1 if fail else 0)
