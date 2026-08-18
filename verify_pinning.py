#!/usr/bin/env python3
"""Verify device-instance pinning on the Cerbo GX.

Takes a baseline snapshot of
  1. every com.victronenergy.* dbus service that exposes /DeviceInstance,
  2. every virtual_* entry under /Settings/Devices,
  3. the Signal K data model keys under tanks / electrical / propulsion,
then restarts Node-RED three times (a restart reloads all flows — a
strictly stronger test than an editor redeploy) and confirms that no
snapshot gains a service, changes an instance, adds a settings entry,
or grows a new Signal K path.

PASS criteria (the task's acceptance test): after 3 restarts neither the
Cerbo device list nor the Signal K data model has any new instance.

Precondition: run this only after the Instance Registry flow's audit
reports no RECONVERGE (i.e. the one-time migration to pinned instances
has already converged) — otherwise the first restart legitimately moves
services to their pinned instance and shows up here as CHANGED.

Usage:
  verify_pinning.py            password from CERBO_PASS env, else prompted
Requires: paramiko (pip install paramiko), boat network reachable.
"""
import json
import os
import sys
import time
import getpass

import paramiko

HOST = os.environ.get("CERBO_HOST", "venus.local")
USER = "root"
RESTARTS = 3
SETTLE_TIMEOUT = 180   # max seconds to wait for flows to come back up
SETTLE_EXTRA = 15      # extra seconds after services reappear

SNAPSHOT_CMD = r"""
echo --SERVICES--
for s in $(dbus -y 2>/dev/null | grep -o 'com\.victronenergy\.[A-Za-z0-9_.]*' | sort -u); do
  i=$(dbus -y $s /DeviceInstance GetValue 2>/dev/null | tr -cd 0-9)
  [ -n "$i" ] && echo "$s $i"
done
echo --SETTINGS--
dbus -y com.victronenergy.settings /Settings/Devices GetValue 2>/dev/null | grep -o 'virtual_[A-Za-z0-9_/]*' | sort -u
echo --SIGNALK--
curl -s --max-time 10 http://localhost:3000/signalk/v1/api/vessels/self 2>/dev/null
"""

RESTART_CMD = r"""
svcdir=$(ls -d /service/*node-red* 2>/dev/null | head -1)
if [ -n "$svcdir" ]; then svc -t "$svcdir" && echo RESTARTED "$svcdir"; else echo NO-NODE-RED-SERVICE; fi
"""

COUNT_VIRTUAL_CMD = "dbus -y 2>/dev/null | grep -c virtual_"


def connect():
    password = os.environ.get("CERBO_PASS") or getpass.getpass(f"root@{HOST} password: ")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=password, timeout=15,
              allow_agent=False, look_for_keys=False)
    return c


def run(c, cmd, timeout=60):
    _, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace").strip()
    if err:
        print(f"  (remote stderr: {err[:300]})", file=sys.stderr)
    return out


def sk_keys(raw_json):
    """Path keys (depth 3) under tanks/electrical/propulsion."""
    try:
        d = json.loads(raw_json)
    except Exception:
        return {"(signalk unavailable)"}
    keys = set()

    def walk(prefix, obj, depth):
        if depth > 3 or not isinstance(obj, dict):
            return
        for k, v in obj.items():
            q = f"{prefix}.{k}"
            keys.add(q)
            walk(q, v, depth + 1)

    for top in ("tanks", "electrical", "propulsion"):
        if top in d:
            walk(top, d[top], 1)
    return keys


def snapshot(c):
    out = run(c, SNAPSHOT_CMD, timeout=120)
    services, settings, sk_raw, section = {}, set(), [], None
    for line in out.splitlines():
        t = line.strip()
        if t == "--SERVICES--":
            section = "svc"
            continue
        if t == "--SETTINGS--":
            section = "set"
            continue
        if t == "--SIGNALK--":
            section = "sk"
            continue
        if section == "svc" and t:
            parts = t.rsplit(" ", 1)
            if len(parts) == 2 and parts[1].isdigit():
                services[parts[0]] = int(parts[1])
        elif section == "set" and t:
            settings.add(t)
        elif section == "sk":
            sk_raw.append(line)
    return {"services": services, "settings": settings,
            "sk": sk_keys("\n".join(sk_raw))}


def wait_for_flows(c, expected_virtual):
    deadline = time.time() + SETTLE_TIMEOUT
    while time.time() < deadline:
        time.sleep(10)
        try:
            n = int(run(c, COUNT_VIRTUAL_CMD, timeout=30).strip() or 0)
        except Exception:
            n = 0
        if n >= expected_virtual:
            time.sleep(SETTLE_EXTRA)
            return True
    return False


def diff(base, snap, label):
    problems = []
    for name, inst in snap["services"].items():
        if name not in base["services"]:
            problems.append(f"NEW service {name} (instance {inst})")
        elif base["services"][name] != inst:
            problems.append(f"CHANGED instance {name}: "
                            f"{base['services'][name]} -> {inst}")
    missing = set(base["services"]) - set(snap["services"])
    for name in sorted(missing):
        print(f"  warning [{label}]: service missing (flow not back up?): {name}")
    for entry in sorted(snap["settings"] - base["settings"]):
        problems.append(f"NEW settings entry Devices/{entry}")
    for key in sorted(snap["sk"] - base["sk"]):
        problems.append(f"NEW Signal K path {key}")
    return problems


def main():
    c = connect()
    try:
        print(f"Baseline snapshot from {HOST} ...")
        base = snapshot(c)
        n_virtual = sum(1 for s in base["services"] if "virtual_" in s)
        print(f"  {len(base['services'])} instanced services "
              f"({n_virtual} virtual), {len(base['settings'])} virtual settings "
              f"entries, {len(base['sk'])} Signal K keys")

        all_problems = []
        for i in range(1, RESTARTS + 1):
            print(f"Restart {i}/{RESTARTS}: restarting Node-RED ...")
            print("  " + run(c, RESTART_CMD, timeout=30).strip())
            if not wait_for_flows(c, n_virtual):
                print(f"  warning: only partial flow recovery after "
                      f"{SETTLE_TIMEOUT}s — snapshotting anyway")
            snap = snapshot(c)
            problems = diff(base, snap, f"restart {i}")
            for p in problems:
                print(f"  FAIL [restart {i}]: {p}")
            if not problems:
                print(f"  restart {i}: no new instances, settings or paths")
            all_problems += problems

        print()
        if all_problems:
            print(f"RESULT: FAIL — {len(all_problems)} problem(s); "
                  "instances are not fully pinned.")
            return 1
        print(f"RESULT: PASS — {RESTARTS} restarts, no new instance numbers "
              "on dbus, in settings, or in the Signal K model.")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
