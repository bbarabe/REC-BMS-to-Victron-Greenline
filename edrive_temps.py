#!/usr/bin/env python3
"""Read the Greenline e-drive temperatures off can0 on the Cerbo GX.

Each drive reports three temperatures, all of them in PGN 61453:

    MOSFET      controller output stage   ("MCU/HCU MOSFET" on the MFD)
    MCU / HCU   controller board          ("MCU" port / "HCU" starboard)
    DRIVE       motor                     ("DRIVE")

61453 is quantized to 1 degree, so a sensor sitting on an integer boundary
alternates between two values frame to frame -- exactly what the MFD shows.
The CANopen TPDO 0x28x carries the MOSFET and drive temps as raw floats, so
this prints both: the integers the display shows, and the underlying floats.
There is no float source for the MCU/HCU temp.

Usage: edrive_temps.py [seconds]        (default 5)
Host from CERBO_HOST (default venus.local), password from CERBO_PASS.
"""
import os
import sys
import base64
import paramiko

HOST = os.environ.get("CERBO_HOST", "venus.local")
USER = "root"
DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0

# Runs on the Cerbo. candump is the only bus reader available there; a raw
# SocketCAN socket would need the CAN_EFF_FLAG workaround for no gain, since
# every frame we want is in the 11-bit range or a plain extended ID.
REMOTE = r'''
import subprocess, struct, time, collections, sys

DUR = float(sys.argv[1])
WANT = ('14F00D64', '14F00D65', '28A', '28B', '18A', '18B')
p = subprocess.Popen(['candump', '-tA', 'can0'], stdout=subprocess.PIPE, text=True)
acc = collections.defaultdict(list)
t0 = time.time()
try:
    for line in p.stdout:
        f = line.split()
        # (date) (time)  can0  ID  [len]  bb bb ...
        if len(f) < 6 or f[2] != 'can0':
            continue
        if f[3] in WANT:
            n = int(f[4].strip('[]'))
            acc[f[3]].append(bytes(int(x, 16) for x in f[5:5 + n]))
        if time.time() - t0 > DUR:
            break
finally:
    p.kill()

def show(counter):
    """'18' when steady, '18/19' when the value straddles an integer."""
    items = sorted(counter.items(), key=lambda kv: -kv[1])
    return '/'.join(str(k) for k, _ in sorted(items[:2]))

print('                MOSFET     MCU/HCU       DRIVE      running')
for cid, side in (('14F00D65', 'PORT'), ('14F00D64', 'STBD')):
    d = acc.get(cid)
    if not d:
        print('  %-6s      -- no 61453 frames --' % side)
        continue
    w = [struct.unpack('<4H', x) for x in d]
    # standard J1939 temperature encoding: 0.03125 K/bit, 273 K offset
    c = [collections.Counter(int(x[i] * 0.03125 - 273) for x in w) for i in range(3)]
    st = acc.get('18A' if side == 'PORT' else '18B')
    run = {0: 'stopped', 1: 'RUNNING'}.get(st[-1][0], st[-1].hex()) if st else '?'
    print('  %-6s %9s C %9s C %9s C     %s'
          % (side, show(c[0]), show(c[2]), show(c[1]), run))

print()
print('  0x28x floats (MOSFET / DRIVE only -- no MCU/HCU float exists)')
for cid, side in (('28A', 'PORT'), ('28B', 'STBD')):
    d = acc.get(cid)
    if not d:
        print('  %-6s      -- no 0x%s frames --' % (side, cid))
        continue
    f1 = [struct.unpack('<f', x[0:4])[0] for x in d]
    f2 = [struct.unpack('<f', x[4:8])[0] for x in d]
    print('  %-6s MOSFET %6.2f..%6.2f    DRIVE %6.2f..%6.2f   (n=%d)'
          % (side, min(f1), max(f1), min(f2), max(f2), len(d)))
'''

pw = os.environ.get("CERBO_PASS")
if not pw:
    sys.exit("Set CERBO_PASS in the environment")

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=pw, timeout=10,
          allow_agent=False, look_for_keys=False)

b64 = base64.b64encode(REMOTE.encode()).decode()
cmd = ("echo %s | base64 -d > /var/volatile/tmp/edrive_temps.py && "
       "python3 /var/volatile/tmp/edrive_temps.py %g" % (b64, DUR))
stdin, stdout, stderr = c.exec_command(cmd, timeout=DUR + 30)
print(stdout.read().decode(errors="replace"), end="")
err = stderr.read().decode(errors="replace")
if err.strip():
    print("STDERR:", err, file=sys.stderr)
c.close()
