#!/usr/bin/env python3
"""Read-only SSH helper for a Cerbo GX. Runs a command, prints output.

Usage: cerbo_ssh.py "<command>" [timeout_seconds]
Host from CERBO_HOST (default venus.local), password from CERBO_PASS.
"""
import os, sys, paramiko

HOST = os.environ.get("CERBO_HOST", "venus.local")
USER = "root"
PASS = os.environ.get("CERBO_PASS")
if not PASS:
    sys.exit("Set CERBO_PASS in the environment")

cmd = sys.argv[1]
timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 30

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PASS, timeout=10,
          allow_agent=False, look_for_keys=False)
stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
try:
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
except Exception as e:
    out, err = "", f"(timeout/err: {e})"
print(out)
if err.strip():
    print("STDERR:", err, file=sys.stderr)
c.close()
