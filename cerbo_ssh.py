#!/usr/bin/env python3
"""Read-only SSH helper for Cerbo GX. Runs a command, prints output.
Usage: cerbo_ssh.py "<command>" [timeout_seconds]
"""
import sys, paramiko

HOST = "192.168.50.107"
USER = "root"

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
