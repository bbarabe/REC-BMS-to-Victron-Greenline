#!/usr/bin/env python3
"""Remote NMEA 2000 bus capture controller for the Cerbo GX.

Runs a full-bus `candump -tA can0` on the Cerbo (tmpfs, no SD wear) and pulls
new bytes down incrementally so a capture can be analysed while it is running.

Usage:
  nmea_capture.py start [tag]   start a fresh capture (kills any previous one)
  nmea_capture.py fetch         download bytes appended since the last fetch
  nmea_capture.py mark "text"   annotate the local log with a timestamped marker
  nmea_capture.py status        show remote capture size / frame rate
  nmea_capture.py stop          stop the remote capture (keeps the remote file)

Local capture lands in ./captures/<tag>.log alongside <tag>.state.
Host from CERBO_HOST (default venus.local), password from CERBO_PASS.
"""
import os
import sys
import json
import gzip
import time
import datetime
import paramiko

HOST = os.environ.get("CERBO_HOST", "venus.local")
USER = "root"
REMOTE_DIR = "/var/volatile/tmp/nmeacap"
LOCAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures")
MAX_FETCH = 4 * 1024 * 1024      # cap one fetch so it always returns promptly

# The CZone control plane, not the whole bus: a full-bus dump runs ~1.6 MB/min
# here, too much to pull down between button presses.
FILTERS = ",".join([
    "01F20D00:03FFFF00",   # 127501 Binary Switch Bank Status
    "01F20E00:03FFFF00",   # 127502 Switch Bank Control
    "01F20C00:03FFFF00",   # 127500 Load Controller Connection State/Control
    "00FF0000:03FF0000",   # 65280-65535   proprietary single-frame
    "01FF0000:03FF0000",   # 130816-131071 proprietary fast-packet
    "00EA0000:03FF0000",   # 59904 ISO Request
    "00EE0000:03FF0000",   # 60928 ISO Address Claim
])
# The "-tA " keeps this command line from matching the Node-RED watchdog's
# `pkill -f '[c]andump can0,01F20D00:03FFFF00'`, which would otherwise kill us.
CANDUMP = "candump -tA can0," + FILTERS
# Bracket the first char so `pkill -f` cannot match the shell that runs it.
PKILL_PAT = "[c]andump -tA can0"


def connect():
    pw = os.environ.get("CERBO_PASS")
    if not pw:
        sys.exit("Set CERBO_PASS in the environment")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=pw, timeout=10,
              allow_agent=False, look_for_keys=False)
    return c


def run(c, cmd, timeout=60):
    _, out, err = c.exec_command(cmd, timeout=timeout)
    o = out.read().decode(errors="replace")
    e = err.read().decode(errors="replace")
    return o, e


def state_path(tag):
    return os.path.join(LOCAL_DIR, tag + ".state")


def load_state(tag=None):
    if tag is None:
        p = os.path.join(LOCAL_DIR, "current.tag")
        if not os.path.exists(p):
            sys.exit("No active capture — run: nmea_capture.py start")
        tag = open(p).read().strip()
    return json.load(open(state_path(tag)))


def save_state(st):
    json.dump(st, open(state_path(st["tag"]), "w"), indent=2)
    with open(os.path.join(LOCAL_DIR, "current.tag"), "w") as f:
        f.write(st["tag"])


def cmd_start(argv):
    """start [tag] [full]  —  'full' captures the whole bus, not just the CZone
    control plane. Needed for device discovery: that traffic uses addressed
    (PDU1) messages and the ISO transport protocol, which the CZone filter set
    does not match."""
    full = 'full' in argv
    argv = [a for a in argv if a != 'full']
    tag = argv[0] if argv else "cap" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    os.makedirs(LOCAL_DIR, exist_ok=True)
    c = connect()
    remote = "%s/%s.log" % (REMOTE_DIR, tag)
    run(c, "mkdir -p %s ; pkill -f '%s' ; sleep 1" % (REMOTE_DIR, PKILL_PAT))
    # nohup + every fd redirected to a file, so the SSH channel sees EOF at once
    # and the dump outlives the SSH session. Backgrounding a compound list instead
    # keeps the channel's stdout open and hangs the read.
    cmdline = "candump -tA can0" if full else CANDUMP
    run(c, "nohup %s > %s 2>%s.err < /dev/null &" % (cmdline, remote, remote))
    time.sleep(2)
    o, _ = run(c, "ps w | grep '[c]andump -tA' ; wc -c < %s" % remote)
    st = {"tag": tag, "remote": remote, "offset": 0,
          "started": datetime.datetime.now().isoformat(timespec="seconds")}
    save_state(st)
    open(os.path.join(LOCAL_DIR, tag + ".log"), "w").close()
    c.close()
    print("capture '%s' started -> %s" % (tag, remote))
    print(o.strip())


def cmd_fetch(argv):
    st = load_state(argv[0] if argv else None)
    c = connect()
    # `dd | gzip` over exec, not SFTP: paramiko's SFTP moves this log at well
    # under 100 kB/s from the Cerbo, while the dump itself is highly
    # compressible (~8x), so gzip turns a 90s transfer into a 25s one.
    # dd needs a block-aligned skip, so rewind to the block boundary and drop
    # the overshoot locally.
    blk = 4096
    skip = st["offset"] // blk
    over = st["offset"] - skip * blk
    _, out, _ = c.exec_command(
        "dd if=%s bs=%d skip=%d 2>/dev/null | gzip -1" % (st["remote"], blk, skip),
        timeout=300)
    raw = gzip.decompress(out.read())[over:]
    c.close()
    local = os.path.join(LOCAL_DIR, st["tag"] + ".log")
    with open(local, "ab") as f:
        f.write(raw)
    st["offset"] += len(raw)
    save_state(st)
    print("fetched %d bytes (%d lines); offset now %d -> %s"
          % (len(raw), raw.count(b"\n"), st["offset"], local))


def cmd_mark(argv):
    st = load_state()
    local = os.path.join(LOCAL_DIR, st["tag"] + ".log")
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    with open(local, "a", encoding="utf-8") as f:
        f.write("### MARK (%s) %s\n" % (now, " ".join(argv)))
    print("marked at %s" % now)


def cmd_status(argv):
    st = load_state(argv[0] if argv else None)
    c = connect()
    o, _ = run(c, "ps w | grep '[c]andump -tA' | head -n 3 ; "
                  "echo bytes=$(wc -c < %s) ; echo lines=$(wc -l < %s) ; "
                  "date -u '+remote_utc=%%Y-%%m-%%d %%H:%%M:%%S'" % (st["remote"], st["remote"]))
    c.close()
    print("tag=%s remote=%s local_offset=%d" % (st["tag"], st["remote"], st["offset"]))
    print(o.strip())


def cmd_stop(argv):
    c = connect()
    o, _ = run(c, "pkill -f '%s' && echo stopped || echo 'no capture running'" % PKILL_PAT)
    c.close()
    print(o.strip())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    {"start": cmd_start, "fetch": cmd_fetch, "mark": cmd_mark,
     "status": cmd_status, "stop": cmd_stop}[sys.argv[1]](sys.argv[2:])
