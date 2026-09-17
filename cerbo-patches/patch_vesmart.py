#!/usr/bin/env python3
"""Validate the fix proposed in victronenergy/venus#1674 on a running Cerbo.

Guards btmgmt_protocol.reader() against mgmt event codes it has no Packet for
(0x0004 IndexAdded / 0x0005 IndexRemoved among them) and teaches pin.py to
tolerate the None that now comes back. Backups in /data/vesmart-patch-backup.
Run with --revert to restore.
"""
import os, py_compile, shutil, sys

BT = "/opt/victronenergy/vesmart-server/ext/python-btsocket/btsocket/btmgmt_protocol.py"
PIN = "/opt/victronenergy/vesmart-server/pin.py"
BACKUP = "/data/vesmart-patch-backup"

BT_OLD = (
    "    event_frame = events.get(header.event_code.value)\n"
    "\n"
    "    cmd_params = event_frame.decode(evt_params)\n"
)
BT_NEW = (
    "    event_frame = events.get(header.event_code.value)\n"
    "    if event_frame is None:\n"
    "        logger.debug('Ignoring unhandled mgmt event 0x%04x',\n"
    "                     header.event_code.value)\n"
    "        return None\n"
    "\n"
    "    cmd_params = event_frame.decode(evt_params)\n"
)

PIN_OLD = (
    "\t\t\tpkt: btmgmt_protocol.Response = btmgmt_protocol.reader(data)\n"
    "\t\t\tlogger.debug(\"Received data from btmgmt socket: %s\", pkt)\n"
)
PIN_NEW = (
    "\t\t\tpkt: btmgmt_protocol.Response = btmgmt_protocol.reader(data)\n"
    "\t\t\tif pkt is None:\n"
    "\t\t\t\treturn True\n"
    "\t\t\tlogger.debug(\"Received data from btmgmt socket: %s\", pkt)\n"
)

def backup_path(p):
    return os.path.join(BACKUP, p.lstrip("/").replace("/", "_"))

def revert():
    for p in (BT, PIN):
        b = backup_path(p)
        if not os.path.exists(b):
            print(f"NO BACKUP for {p}"); continue
        shutil.copy2(b, p); print(f"reverted {p}")
    drop_pyc(); print("REVERTED")

def drop_pyc():
    for d in (os.path.join(os.path.dirname(BT), "__pycache__"),
              os.path.join(os.path.dirname(PIN), "__pycache__")):
        if os.path.isdir(d):
            for f in os.listdir(d):
                os.remove(os.path.join(d, f))
            print(f"cleared {d}")

def apply():
    os.makedirs(BACKUP, exist_ok=True)
    for p, old, new in ((BT, BT_OLD, BT_NEW), (PIN, PIN_OLD, PIN_NEW)):
        src = open(p).read()
        if new in src:
            print(f"ALREADY PATCHED {p}"); continue
        if src.count(old) != 1:
            sys.exit(f"ABORT: expected exactly 1 match in {p}, found {src.count(old)}")
        b = backup_path(p)
        if not os.path.exists(b):
            shutil.copy2(p, b); print(f"backed up {p} -> {b}")
        open(p, "w").write(src.replace(old, new))
        print(f"patched {p}")
    for p in (BT, PIN):
        py_compile.compile(p, doraise=True, cfile="/tmp/_chk.pyc")
        print(f"syntax OK {p}")
    os.path.exists("/tmp/_chk.pyc") and os.remove("/tmp/_chk.pyc")
    drop_pyc()
    print("APPLIED")

revert() if "--revert" in sys.argv else apply()
