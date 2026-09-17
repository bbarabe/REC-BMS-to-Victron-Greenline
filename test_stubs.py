#!/usr/bin/env python3
"""
test_stubs.py — the off-boat stand-ins shared by the driver tests.

Stubs dbus, velib_python (vedbus, settingsdevice), GLib and the SocketCAN
socket so a driver module can be imported and constructed for real without a
Cerbo. The fake CAN socket ENFORCES each driver's own CAN_RAW_FILTER, so a
frame the kernel would drop cannot pass here either.

Not a test: `python test_drivers.py` and `python test_solar_priority.py` are.
"""
import json, os, sys, types, struct

REPO = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- stub dbus
class _Err(Exception):
    pass


class FakeBus:
    """One shared localsettings store across every 'connection'."""
    store = {}          # path -> value
    names = {}          # bus name -> FakeService

    def __init__(self, private=False):
        self.closed = False

    def close(self):
        self.closed = True

    def list_names(self):
        return list(FakeBus.names)

    def add_signal_receiver(self, *a, **kw):
        return None

    def call_blocking(self, svc, path, iface, method, sig, args, timeout=None):
        if svc == "com.victronenergy.settings":
            if method == "AddSetting":
                group, leaf, default, itype, lo, hi = args
                p = "/Settings/%s/%s" % (group.strip("/"), leaf)
                FakeBus.store.setdefault(p, default)
                return 0
            if method == "GetValue":
                if path not in FakeBus.store:
                    raise _Err("no such setting " + path)
                return FakeBus.store[path]
            if method == "SetValue":
                # localsettings will not let two /Settings/Devices entries
                # claim one instance: it reports success and keeps the old
                # value. Reproduce that, it is what bit us on the boat.
                if path.endswith("/ClassAndVrmInstance"):
                    for p2, v2 in FakeBus.store.items():
                        if (p2 != path and p2.endswith("/ClassAndVrmInstance")
                                and v2 == args[0]):
                            return 0
                FakeBus.store[path] = args[0]
                return 0
            if method == "RemoveSettings":
                return 0
        # reading another service's /DeviceInstance
        s = FakeBus.names.get(svc)
        if s is not None and method == "GetValue":
            return s.values.get(path)
        raise _Err("no route %s %s %s" % (svc, path, method))


dbus = types.ModuleType("dbus")
dbus.SystemBus = lambda private=False: FakeBus(private)
dbus.SessionBus = dbus.SystemBus
mainloop = types.ModuleType("dbus.mainloop")
glibmod = types.ModuleType("dbus.mainloop.glib")
glibmod.DBusGMainLoop = lambda set_as_default=False: None
dbus.mainloop = mainloop
mainloop.glib = glibmod
sys.modules["dbus"] = dbus
sys.modules["dbus.mainloop"] = mainloop
sys.modules["dbus.mainloop.glib"] = glibmod

# ----------------------------------------------------------------- stub GLib
class GLib:
    IO_IN, IO_ERR, IO_HUP = 1, 8, 16
    timers = []

    @staticmethod
    def io_add_watch(*a, **kw):
        return 1

    @staticmethod
    def timeout_add(ms, fn, *a):
        GLib.timers.append((fn, a))
        return len(GLib.timers)

    @staticmethod
    def timeout_add_seconds(s, fn, *a):
        GLib.timers.append((fn, a))
        return len(GLib.timers)

    @staticmethod
    def idle_add(fn, *a):
        fn(*a)
        return 1

    @staticmethod
    def source_remove(i):
        return True

    class MainLoop:
        def run(self):
            pass


gi = types.ModuleType("gi")
rep = types.ModuleType("gi.repository")
rep.GLib = GLib
gi.repository = rep
sys.modules["gi"] = gi
sys.modules["gi.repository"] = rep

# --------------------------------------------------------------- stub velib
class FakeService:
    def __init__(self, name, bus=None, register=True):
        self.name = name
        self.bus = bus
        self.values = {}
        self.registered = register
        if register:
            FakeBus.names[name] = self

    def register(self):
        self.registered = True
        FakeBus.names[self.name] = self

    def add_path(self, path, value, writeable=False, onchangecallback=None,
                 gettextcallback=None):
        if path in self.values:
            raise AssertionError("duplicate path %s on %s" % (path, self.name))
        self.values[path] = value
        if gettextcallback is not None:
            gettextcallback(path, value)      # must not raise on None
        if writeable:
            self.__dict__.setdefault("cbs", {})[path] = onchangecallback

    def write(self, path, value):
        """Simulate an external D-Bus write. velib's SetValue returns OK
        without calling the change callback when the value is the one the
        path already holds -- reproduce that, it bit the sustain re-assert."""
        cb = self.__dict__.get("cbs", {}).get(path)
        assert cb is not None, "%s is not writeable" % path
        if value == self.values[path]:
            return True
        if cb(path, value):
            self.values[path] = value
            return True
        return False

    def __getitem__(self, p):
        return self.values[p]

    def __setitem__(self, p, v):
        assert p in self.values, "set of unknown path %s on %s" % (p, self.name)
        self.values[p] = v

    def __contains__(self, p):
        return p in self.values

    # velib's `with service as s:` batches a tick's changes into one
    # ItemsChanged. The fake just applies them; count() lets a test see how
    # many values a tick actually touched.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __del__(self):
        FakeBus.names.pop(self.name, None)


vedbus = types.ModuleType("vedbus")
vedbus.VeDbusService = FakeService
sys.modules["vedbus"] = vedbus


class FakeSettingsDevice:
    def __init__(self, bus, supported, cb, timeout=0):
        self._paths = {}
        for alias, opts in supported.items():
            path, default = opts[0], opts[1]
            FakeBus.store.setdefault(path, default)
            self._paths[alias] = path

    def __getitem__(self, alias):
        return FakeBus.store[self._paths[alias]]

    def __setitem__(self, alias, value):
        FakeBus.store[self._paths[alias]] = value


sd = types.ModuleType("settingsdevice")
sd.SettingsDevice = FakeSettingsDevice
sys.modules["settingsdevice"] = sd

# ------------------------------------------------------------- stub CAN sock
import socket as _socket
if not hasattr(_socket, "AF_CAN"):
    _socket.AF_CAN = 29
    _socket.PF_CAN = 29
    _socket.CAN_RAW = 1
    _socket.SOL_CAN_RAW = 101
    _socket.CAN_RAW_FILTER = 1
    _socket.CAN_EFF_FLAG = -0x80000000      # as on armv7l: NEGATIVE
    _socket.CAN_EFF_MASK = 0x1FFFFFFF

_real_socket = _socket.socket


class FakeCanSocket:
    def __init__(self, *a, **kw):
        self.filters = None
        self.queue = []

    def setsockopt(self, level, opt, val):
        self.filters = val

    def bind(self, addr):
        pass

    def setblocking(self, b):
        pass

    def fileno(self):
        return 0

    def close(self):
        pass

    def feed(self, cid, data, extended=True):
        flag = _socket.CAN_EFF_FLAG & 0xFFFFFFFF if extended else 0
        self.queue.append(struct.pack("=IB3x8s", (cid | flag) & 0xFFFFFFFF,
                                      len(data), data.ljust(8, b"\x00")))

    def recv(self, n):
        if not self.queue:
            raise BlockingIOError()
        return self.queue.pop(0)


def fake_socket(family=None, *a, **kw):
    if family == _socket.AF_CAN:
        return FakeCanSocket()
    return _real_socket(family, *a, **kw)


_socket.socket = fake_socket


def filter_entries(blob):
    return [struct.unpack_from("=II", blob, i) for i in range(0, len(blob), 8)]


# _find_velib() probes the filesystem; vedbus/settingsdevice are already
# stubbed into sys.modules, so let the first candidate "exist".
_real_isfile = os.path.isfile
os.path.isfile = lambda p: p.replace("\\", "/").endswith(
    "/data/velib_python/vedbus.py") or _real_isfile(p)


def load(path, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def run_timers(n=1):
    for _ in range(n):
        for fn, a in list(GLib.timers):
            fn(*a)


ok, fail = [], []


def check(label, cond, extra=""):
    (ok if cond else fail).append(label)
    print(("  PASS  " if cond else "  FAIL  ") + label + (" " + extra if extra else ""))
