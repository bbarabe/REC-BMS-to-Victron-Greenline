#!/usr/bin/env python3
"""
test_shore_input.py — off-boat checks for Solar Priority's shore AC input.

    python test_shore_input.py

The pure resolver (which Quattro AC input is shore), then the real
SolarPriorityDriver under the test_stubs stand-ins and a fake DbusMonitor:
the relay writes must land on the resolved input only, nothing may move
while the input is unknown, and a rewire must release the input left behind.
Exit status is 0 only if everything passes.
"""
from test_stubs import *   # noqa: F401,F403
import types


class FakeMonitor:
    """The slice of velib's DbusMonitor the driver uses, over plain dicts."""
    inst = None

    def __init__(self, tree, valueChangedCallback=None, deviceAddedCallback=None,
                 deviceRemovedCallback=None, **kw):
        FakeMonitor.inst = self
        self.tree, self.changed = tree, valueChangedCallback
        self.services = {}        # name -> (instance, {path: value})
        self.writes = []          # (name, path, value)

    def add(self, name, instance, values):
        self.services[name] = (instance, dict(values))

    def get_service_list(self, classfilter=None):
        return {n: i for n, (i, _) in self.services.items()
                if classfilter is None or n.startswith(classfilter)}

    def get_device_instance(self, name):
        return self.services[name][0]

    def get_value(self, name, path, default=None):
        return self.services.get(name, (None, {}))[1].get(path, default)

    def set_value(self, name, path, value):
        self.writes.append((name, path, value))
        return 0

    def set_value_async(self, name, path, value, reply_handler=None, error_handler=None):
        self.writes.append((name, path, value))

    def push(self, name, path, value):
        self.services[name][1][path] = value
        self.changed(name, path, None, {"Value": value}, self.services[name][0])


dm = types.ModuleType("dbusmonitor")
dm.DbusMonitor = FakeMonitor
sys.modules["dbusmonitor"] = dm
SP = load(os.path.join(REPO, "dbus-recbms", "solar_priority.py"), "solar_priority")
R = SP.resolve_shore_input

print("\n=== shore AC input: the resolver ===")
check("a pinned input wins over everything",
      R(2, (3, 0), 0, (1, 0), 1) == (2, "configured") and R(1, (0, 3), 1, (0, 1), 2) == (1, "configured"))
check("GX types decide when exactly one input is grid or shore",
      R("auto", (0, 3), 240, (None, None), None) == (2, "gx input type") and
      R("auto", (3, 0), 0, (1, 0), None) == (1, "gx input type") and
      R("auto", (2, 1), 0, (1, 1), None) == (2, "gx input type"))
check("... and they override a settled input: the rewire",
      R("auto", (0, 3), 0, (1, 0), 1) == (2, "gx input type"))
check("a settled input is kept on an island (ActiveInput 240, nothing available)",
      R("auto", (None, None), 240, (0, 0), 2) == (2, "kept") and
      R("auto", (0, 0), 240, (None, None), 1) == (1, "kept"))
check("both inputs typed shore: the settled input stands", R("auto", (3, 3), 1, (1, 1), 1) == (1, "kept"))
check("a fresh start reads the accepted input",
      R("auto", (0, 0), 1, (0, 1), None) == (2, "accepted input") and
      R("auto", (None, None), 0, (1, 0), None) == (1, "accepted input"))
check("... then the only input with AC on it", R("auto", (0, 0), 240, (0, 1), None) == (2, "only input available"))
check("a generator input is never picked from the live facts",
      R("auto", (2, 0), 0, (1, 0), None) == (None, "unresolved") and
      R("auto", (2, 0), 240, (1, 1), None) == (2, "only input available"))
check("nothing known: unresolved, never a guess",
      R("auto", (0, 0), 240, (1, 1), None) == (None, "unresolved") and
      R("auto", (None, None), None, (None, None), None) == (None, "unresolved"))

print("\n=== shore AC input: the driver ===")
VEBUS, SETTINGS = "com.victronenergy.vebus.ttyS4", "com.victronenergy.settings"
IGN1, IGN2 = "/Ac/Control/IgnoreAcIn1", "/Ac/Control/IgnoreAcIn2"
M = [9_000.0]
SP.time = types.SimpleNamespace(time=lambda: 1_800_000_000.0 + M[0], monotonic=lambda: M[0])


def driver(ac_in="auto", types_=(None, None), active=None, remembered=0):
    FakeBus.store.clear()
    FakeBus.store["/Settings/SolarPriority/ShoreInput"] = remembered
    cfg = SP.Config(os.path.join(REPO, "dbus-recbms", "solar_priority.ini"))
    cfg.ac_in = ac_in
    real = SP.DbusMonitor

    class Seeded(FakeMonitor):
        def __init__(self, *a, **kw):
            FakeMonitor.__init__(self, *a, **kw)
            self.add(VEBUS, 276, {"/Ac/ActiveIn/ActiveInput": active})
            self.add(SETTINGS, None, {"/Settings/SystemSetup/AcInput1": types_[0],
                                      "/Settings/SystemSetup/AcInput2": types_[1]})
    SP.DbusMonitor = Seeded
    try:
        d = SP.SolarPriorityDriver(cfg)
    finally:
        SP.DbusMonitor = real
    return d, FakeMonitor.inst


def tick(d, n=1):
    for _ in range(n):
        M[0] += 1
        d._tick()


check("the shipped ini asks for auto", SP.Config(os.path.join(REPO, "dbus-recbms", "solar_priority.ini")).ac_in == "auto")

d, m = driver(types_=(0, 3), active=1)
tick(d)
check("GX says AC in 2 is shore: resolved 2, ActiveInput 1 means shore",
      d.shore_input == 2 and d.inp.feed_shore == 1 and d.sw["/SolarPriority/ShoreInput"] == 2 and
      d.sw["/SolarPriority/ShoreInputReason"] == "gx input type" and
      FakeBus.store["/Settings/SolarPriority/ShoreInput"] == 2)
check("the engine's own shore command goes to AC in 2 and never to AC in 1",
      m.writes and all(w[1] != IGN1 for w in m.writes), str(m.writes))
del m.writes[:]
d._safe_start()
check("safe start releases the resolved input only", m.writes == [(VEBUS, IGN2, 0)], str(m.writes))
del m.writes[:]
d._shutdown()
check("shutdown releases the resolved input only",
      [w for w in m.writes if w[0] == VEBUS] == [(VEBUS, IGN2, 0)], str(m.writes))

d, m = driver(types_=(0, 0), active=240)
tick(d, 3)
check("nothing known: unresolved, the engine is not run and says why",
      d.shore_input is None and d.sw["/SolarPriority/ShoreInput"] is None and
      "not resolved" in d.sw["/SolarPriority/Status"] and d.engine.st["state"] == "shore")
d._safe_start()
check("... and a safe start releases BOTH inputs (a crashed process may have left either)",
      sorted(w[1] for w in m.writes) == [IGN1, IGN2] and all(w[2] == 0 for w in m.writes), str(m.writes))
m.push(VEBUS, "/Ac/ActiveIn/ActiveInput", 1)
tick(d)
check("the Quattro accepts AC in 2: resolved from the accepted input",
      d.shore_input == 2 and d.shore_input_reason == "accepted input")
m.push(VEBUS, "/Ac/ActiveIn/ActiveInput", 240)
tick(d)
check("an island afterwards keeps it", d.shore_input == 2 and d.shore_input_reason == "kept")

d, m = driver(types_=(0, 0), active=240, remembered=2)
check("a restart on an island remembers the input it was ignoring",
      d.shore_input == 2 and d.shore_input_reason == "kept")

d, m = driver(types_=(3, 0), active=0)
tick(d)
check("shore on AC in 1", d.shore_input == 1 and d.inp.feed_shore == 0)
d.engine.st["state"] = "solar"
del m.writes[:]
m.push(SETTINGS, "/Settings/SystemSetup/AcInput1", 0)
m.push(SETTINGS, "/Settings/SystemSetup/AcInput2", 3)
tick(d)
check("the rewire: AC in 1 released, engine back on shore, now on AC in 2",
      (VEBUS, IGN1, 0) in m.writes and d.shore_input == 2 and d.inp.feed_shore == 1 and
      d.engine.st["state"] == "shore", "%s %s" % (m.writes, d.engine.st["state"]))

check("... and the new input is released too (the engine believes shore is already sent)",
      (VEBUS, IGN2, 0) in m.writes, str(m.writes))

d, m = driver(ac_in=1, types_=(0, 3), active=1, remembered=2)
tick(d)
check("a pinned input ignores the GX types", d.shore_input == 1 and d.shore_input_reason == "configured")
check("... and is remembered, so a later auto cannot come up 'kept' on a stale input",
      FakeBus.store["/Settings/SolarPriority/ShoreInput"] == 1)

d, m = driver(types_=(0, 3), active=1)
tick(d)
d.engine.st["state"] = "solar"
del m.writes[:]
d._tick_inner = lambda s: 1 / 0
check("a tick that raises keeps the timer and forces shore",
      d._tick() is True and d.engine.st["state"] == "shore" and (VEBUS, IGN2, 0) in m.writes, str(m.writes))

d, m = driver(types_=(0, 0), active=240)
tick(d)
del m.writes[:]
d._shutdown()
check("shutdown while unresolved releases both inputs",
      sorted(w[1] for w in m.writes if w[0] == VEBUS) == [IGN1, IGN2], str(m.writes))

print("\n=== prefer renewable: the driver's writer (4.2.0) ===")
PRE = "/Dc/0/PreferRenewableEnergy"
MPPT = "com.victronenergy.solarcharger.ttyS5"
d, m = driver(types_=(0, 3), active=1)
m.push(VEBUS, PRE, 1)
tick(d)
del m.writes[:]
d._write_prefer(d._ms(), 1)
check("toggle already reads what is wanted: nothing written", [w for w in m.writes if w[1] == PRE] == [])
d._write_prefer(d._ms(), 0)
check("night: charge now written once", [w for w in m.writes if w[1] == PRE] == [(VEBUS, PRE, 0)], str(m.writes))
d._write_prefer(d._ms(), 0)
check("... and not again within the minute, whatever it reads", len([w for w in m.writes if w[1] == PRE]) == 1)
M[0] += 61
d._write_prefer(d._ms(), 0)
check("still reading the old value a minute on: written again", len([w for w in m.writes if w[1] == PRE]) == 2)
m.push(VEBUS, PRE, 2)
M[0] += 61
d._write_prefer(d._ms(), 0)
check("a value that is neither 0 nor 1 is never written over", len([w for w in m.writes if w[1] == PRE]) == 2)
d, m = driver(types_=(0, 3), active=1)
tick(d)
d._write_prefer(d._ms(), 0)
check("a toggle this firmware does not publish is never written blind", [w for w in m.writes if w[1] == PRE] == [])
# end to end: a dark array for DUSK_MS, Solar Priority on -> charge now
d, m = driver(types_=(0, 3), active=1)
m.add(MPPT, 278, {"/Pv/V": 0.3, "/Yield/Power": 0.0, "/MppOperationMode": 0})
d._device_added(MPPT, 278)
m.push(VEBUS, PRE, 1)
d.inp.enabled = True
tick(d, 310)
check("the engine's night reaches the Quattro through the driver",
      d.sw["/SolarPriority/Daylight"] == 0 and d.sw["/SolarPriority/PreferRenewable"] == 0
      and (VEBUS, PRE, 0) in m.writes, "%s %s" % (d.sw["/SolarPriority/Daylight"], [w for w in m.writes if w[1] == PRE]))
d.inp.enabled = False
del m.writes[:]
M[0] += 61
tick(d, 2)
check("Solar Priority off: the toggle is left alone",
      d.sw["/SolarPriority/PreferRenewable"] is None and [w for w in m.writes if w[1] == PRE] == [])

M0 = M[0]
d, m = driver(types_=(0, 3), active=1)
t0 = d._ms()
SP.time = types.SimpleNamespace(time=lambda: 1_700_000_000.0, monotonic=lambda: M[0])   # wall clock steps back years
M[0] += 5
check("the engine clock ignores a wall-clock step and sits clear of 0",
      d._ms() - t0 == 5000 and t0 > 10 ** 11, "%s %s" % (t0, d._ms()))

print("\n%d passed, %d failed" % (len(ok), len(fail)))
for f in fail:
    print("  FAILED: " + f)
sys.exit(1 if fail else 0)
