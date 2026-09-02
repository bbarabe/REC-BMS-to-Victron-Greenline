#!/usr/bin/env python3
"""Holds ONE live SSH transport to the Cerbo; serves commands over a unix socket.

Started by the `cerbo` wrapper -- you should not need to run it by hand.

Why a daemon: the Cerbo has almost no SSH headroom. One connection per command,
or a retry loop after a refusal, exhausts sshd and it stops answering for many
minutes. Here a single paramiko transport is opened once and every command runs
as a new *channel* on it (cheap), including SFTP for get/put.

Protocol: newline-delimited JSON both ways.
  -> {"op":"run","cmd":"uptime","timeout":60}
  -> {"op":"get","remote":"/x","local":"./x"} | {"op":"put",...}
  -> {"op":"ping"} | {"op":"stop"}
  <- {"ok":true,"out":"...","err":"...","rc":0}
"""
import json, os, socket, socketserver, sys, threading, time, traceback
import paramiko

HOST = os.environ["CERBO_HOST"]
PASS = os.environ["CERBO_PASS"]
SOCK = sys.argv[1]
IDLE_EXIT = float(os.environ.get("CERBO_IDLE_EXIT", "1800"))

_lock = threading.Lock()          # one channel at a time: this box is small
_last = time.time()
_client = None


def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username="root", password=PASS, timeout=20,
              allow_agent=False, look_for_keys=False, banner_timeout=30)
    c.get_transport().set_keepalive(30)
    return c


def alive():
    t = _client.get_transport() if _client else None
    return bool(t and t.is_active())


def handle(req):
    global _last
    _last = time.time()
    op = req.get("op", "run")
    if op == "ping":
        return {"ok": True, "alive": alive(), "host": HOST}
    if op == "stop":
        threading.Timer(0.2, lambda: os._exit(0)).start()
        return {"ok": True, "out": "stopping"}
    if not alive():
        return {"ok": False, "err": "transport dead; run `cerbo down` then `cerbo up`"}

    with _lock:
        if op == "run":
            _, out, err = _client.exec_command(req["cmd"],
                                               timeout=req.get("timeout", 60))
            o = out.read().decode(errors="replace")
            e = err.read().decode(errors="replace")
            return {"ok": True, "out": o, "err": e,
                    "rc": out.channel.recv_exit_status()}
        if op in ("get", "put"):
            sftp = _client.open_sftp()          # same transport, new channel
            try:
                if op == "get":
                    sftp.get(req["remote"], req["local"])
                else:
                    sftp.put(req["local"], req["remote"])
            finally:
                sftp.close()
            return {"ok": True, "out": f"{op} done\n"}
    return {"ok": False, "err": f"unknown op {op!r}"}


class H(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            if not line.strip():
                continue
            try:
                resp = handle(json.loads(line))
            except Exception:
                resp = {"ok": False, "err": traceback.format_exc(limit=3)}
            self.wfile.write((json.dumps(resp) + "\n").encode())
            self.wfile.flush()


class Srv(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    try:
        _client = connect()
    except Exception as e:
        print(f"CONNECT-FAILED {e}", file=sys.stderr)
        sys.exit(1)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = Srv(SOCK, H)
    os.chmod(SOCK, 0o600)

    def reaper():
        while True:
            time.sleep(15)
            if time.time() - _last > IDLE_EXIT or not alive():
                os._exit(0)
    threading.Thread(target=reaper, daemon=True).start()
    print("READY", flush=True)
    srv.serve_forever()
