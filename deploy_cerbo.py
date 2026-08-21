#!/usr/bin/env python3
"""
deploy_cerbo.py — ship a standalone driver to the Cerbo over ONE SSH session.

    python deploy_cerbo.py recbms                 # upload + svc -t + verify
    python deploy_cerbo.py solarpriority --install   # first install (install.sh)
    python deploy_cerbo.py recbms solarpriority   # several, one session
    python deploy_cerbo.py czone --verify-only    # no upload, no restart
    python deploy_cerbo.py recbms --dry-run       # show what would change

Encodes the rules in CLAUDE.md so nobody re-derives them:
  * one SSH session per run, 30 s keepalive, NEVER retries a failed connect
    (repeated connects exhaust the Cerbo and it drops off the network)
  * the on-boat config (calibration!) is diffed against the repo's HEAD copy
    BEFORE anything is overwritten; a value difference aborts unless
    --force-config, a comment-only difference is fine
  * every overwritten file is backed up on the boat as <file>.bak-<tag>
  * only the requested service is restarted; verification re-reads the
    shipped VERSION after the restart instead of assuming the copy landed
  * Node-RED flows are NOT deployable here (import them in the NR UI)

Host and password come from CERBO_HOST / CERBO_PASS (never from a file).
"""
import argparse
import difflib
import os
import posixpath
import re
import subprocess
import sys
import time

import paramiko

REPO = os.path.dirname(os.path.abspath(__file__))

# What each deployable is made of. 'files' are repo-relative, uploaded to
# /data/<dir>/<same relative path>. 'configs' are value-guarded. 'verify'
# lists (service, path) D-Bus reads printed after the restart.
PACKAGES = {
    "recbms": {
        "dir": "dbus-recbms",
        "service": "dbus-recbms",
        "install_arg": "recbms",
        "version_file": "dbus_recbms.py",
        "files": ["dbus_recbms.py", "config.ini", "README.md", "install.sh",
                  "uninstall.sh", "service/run", "service/log/run"],
        "configs": ["config.ini"],
        "verify": [
            ("com.victronenergy.battery.recbms", "/Mgmt/ProcessVersion"),
            ("com.victronenergy.battery.recbms", "/RecBms/Phase"),
            ("com.victronenergy.battery.recbms", "/RecBms/TargetChargeVoltage"),
            ("com.victronenergy.battery.recbms", "/Info/MaxChargeVoltage"),
            ("com.victronenergy.battery.recbms", "/RecBms/SolarLead"),
            ("com.victronenergy.battery.recbms", "/RecBms/LeadFault"),
            ("com.victronenergy.system", "/Control/EffectiveChargeVoltage"),
            ("com.victronenergy.system", "/ActiveBmsInstance"),
        ],
    },
    "solarpriority": {
        "dir": "dbus-recbms",
        "service": "dbus-solarpriority",
        "install_arg": "solarpriority",
        "version_file": "solar_priority.py",
        "files": ["solar_priority.py", "solar_priority.ini", "README.md",
                  "install.sh", "uninstall.sh",
                  "service-solarpriority/run", "service-solarpriority/log/run"],
        "configs": ["solar_priority.ini"],
        # the Node-RED flow's virtual switches must be gone (both write
        # IgnoreAcIn1, and a zombie would hold instance 221)
        "preflight": ("dbus -y 2>/dev/null | grep -c 'virtual_sp_' || true",
                      "0", "Node-RED Solar Priority flow services still on the bus "
                           "(virtual_sp_*): disable the flow tab + Deploy, restart "
                           "signalk-server if they linger"),
        "verify": [
            ("com.victronenergy.switch.solarpriority", "/Mgmt/ProcessVersion"),
            ("com.victronenergy.switch.solarpriority", "/DeviceInstance"),
            ("com.victronenergy.switch.solarpriority", "/SolarPriority/State"),
            ("com.victronenergy.switch.solarpriority", "/SolarPriority/Status"),
            ("com.victronenergy.switch.solarpriority", "/SwitchableOutput/output_1/State"),
            ("com.victronenergy.switch.solarpriority", "/SwitchableOutput/output_2/Dimming"),
        ],
    },
    "czone": {
        "dir": "dbus-czone",
        "service": "dbus-czone",
        "install_arg": "",
        "version_file": "dbus_czone.py",
        "files": ["dbus_czone.py", "config.ini", "README.md", "install.sh",
                  "uninstall.sh", "service/run", "service/log/run"],
        "configs": ["config.ini"],
        "verify": [
            ("com.victronenergy.switch.czone", "/Mgmt/ProcessVersion"),
            ("com.victronenergy.switch.czone", "/DeviceInstance"),
            ("com.victronenergy.switch.czone", "/Connected"),
        ],
    },
}
EXEC_SUFFIXES = (".py", ".sh", "/run")


def die(msg, code=2):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(code)


def ini_values(text):
    """Value lines only (no comments / blanks / whitespace) for the guard."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in ";#":
            continue
        out.append(re.sub(r"\s*[;#].*$", "", s) if "=" in s else s)
    return out


def git_head(relpath):
    r = subprocess.run(["git", "-C", REPO, "show", "HEAD:" + relpath.replace(os.sep, "/")],
                       capture_output=True, text=True, encoding="utf-8")
    return r.stdout if r.returncode == 0 else None


def local_version(pkg):
    p = os.path.join(REPO, pkg["dir"], pkg["version_file"])
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', open(p, encoding="utf-8").read(), re.M)
    return m.group(1) if m else "?"


class Cerbo:
    def __init__(self, host, password):
        self.c = paramiko.SSHClient()
        self.c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self.c.connect(host, username="root", password=password, timeout=15,
                           allow_agent=False, look_for_keys=False)
        except Exception as e:
            die("cannot connect to %s (%s). Do NOT retry in a loop — wait a few "
                "minutes; repeated connects are what knock the Cerbo offline." % (host, e))
        self.c.get_transport().set_keepalive(30)
        self.sftp = None

    def run(self, cmd, timeout=60):
        _, out, err = self.c.exec_command(cmd, timeout=timeout)
        o = out.read().decode(errors="replace")
        e = err.read().decode(errors="replace")
        return o, e

    def cat(self, path):
        o, e = self.run("cat '%s' 2>/dev/null" % path)
        return o if not e.strip() else o

    def exists(self, path):
        o, _ = self.run("[ -e '%s' ] && echo yes || echo no" % path)
        return o.strip() == "yes"

    def put(self, local, remote):
        if self.sftp is None:
            self.sftp = self.c.open_sftp()
        self.run("mkdir -p '%s'" % posixpath.dirname(remote))
        self.sftp.put(local, remote)
        if remote.endswith(EXEC_SUFFIXES):
            self.run("chmod 755 '%s'" % remote)

    def dbus_get(self, service, path):
        o, e = self.run("dbus -y %s %s GetValue 2>&1" % (service, path), timeout=15)
        lines = (o or e).strip().splitlines()
        return lines[-1].strip() if lines else ""   # dbus CLI errors are tracebacks; the last line says it

    def close(self):
        if self.sftp:
            self.sftp.close()
        self.c.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("packages", nargs="+", choices=sorted(PACKAGES))
    ap.add_argument("--install", action="store_true", help="first install: run install.sh instead of svc -t")
    ap.add_argument("--no-restart", action="store_true", help="upload only")
    ap.add_argument("--verify-only", action="store_true", help="no upload, no restart")
    ap.add_argument("--dry-run", action="store_true", help="diff and plan only")
    ap.add_argument("--force-config", action="store_true",
                    help="overwrite a live config whose VALUES differ from the repo's HEAD copy")
    ap.add_argument("--tag", default=time.strftime("%Y%m%d-%H%M"), help="backup suffix (.bak-<tag>)")
    ap.add_argument("--settle", type=float, default=8.0, help="seconds to wait after restart before verifying")
    args = ap.parse_args()

    host = os.environ.get("CERBO_HOST")
    pw = os.environ.get("CERBO_PASS")
    if not host or not pw:
        die("set CERBO_HOST and CERBO_PASS in the environment (never in a file)")

    # --- local preflight (no network) ---
    plan = []
    for name in args.packages:
        pkg = PACKAGES[name]
        for f in pkg["files"]:
            lp = os.path.join(REPO, pkg["dir"], f)
            if not os.path.isfile(lp):
                die("missing local file %s" % lp)
        if pkg["version_file"].endswith(".py"):
            r = subprocess.run([sys.executable, "-m", "py_compile",
                                os.path.join(REPO, pkg["dir"], pkg["version_file"])])
            if r.returncode:
                die("%s does not compile" % pkg["version_file"])
        plan.append((name, pkg))
        print("== %s: local %s v%s -> /data/%s (service %s)" % (
            name, pkg["version_file"], local_version(pkg), pkg["dir"], pkg["service"]))

    cb = Cerbo(host, pw)
    try:
        rc = 0
        for name, pkg in plan:
            base = "/data/" + pkg["dir"]
            print("\n#### %s" % name)
            o, _ = cb.run("grep -m1 '^VERSION' %s/%s 2>/dev/null; svstat /service/%s 2>&1" % (
                base, pkg["version_file"], pkg["service"]))
            print("live: " + o.strip().replace("\n", " | "))

            if args.verify_only:
                verify(cb, pkg, base)
                continue

            pf = pkg.get("preflight")
            if pf:
                o, _ = cb.run(pf[0])
                if o.strip() != pf[1]:
                    print("!! preflight failed (%r): %s. Skipping %s." % (o.strip(), pf[2], name))
                    rc = 4
                    continue
                print("   preflight ok")

            # --- config guard ---
            uploads = []
            for f in pkg["files"]:
                lp = os.path.join(REPO, pkg["dir"], f)
                rp = base + "/" + f
                local_txt = open(lp, "rb").read()
                live_txt = cb.cat(rp).encode() if cb.exists(rp) else None
                if f in pkg["configs"] and live_txt is not None:
                    head = git_head(pkg["dir"] + "/" + f)
                    live_vals = ini_values(live_txt.decode(errors="replace"))
                    head_vals = ini_values(head) if head is not None else None
                    new_vals = ini_values(local_txt.decode("utf-8"))
                    if head_vals is not None and live_vals != head_vals:
                        d = "\n".join(difflib.unified_diff(head_vals, live_vals, "repo HEAD", "live", lineterm=""))
                        print("!! live %s has on-boat VALUE edits not in the repo:\n%s" % (f, d))
                        if not args.force_config:
                            print("!! fold them into the repo first, or pass --force-config. Skipping %s." % name)
                            rc = 3
                            uploads = None
                            break
                    if live_vals == new_vals:
                        print("   %-34s unchanged (values)" % f)
                        continue
                    print("   %-34s CONFIG values change:\n%s" % (f, "\n".join(
                        "      " + l for l in difflib.unified_diff(live_vals, new_vals, "live", "new", lineterm=""))))
                    uploads.append((lp, rp, True))
                elif live_txt is not None and live_txt.replace(b"\r\n", b"\n") == local_txt.replace(b"\r\n", b"\n"):
                    print("   %-34s unchanged" % f)
                else:
                    print("   %-34s %s" % (f, "UPDATE" if live_txt is not None else "NEW"))
                    uploads.append((lp, rp, live_txt is not None))
            if uploads is None:
                continue

            if args.dry_run:
                print("-- dry run: %d file(s) would be uploaded; %s" % (
                    len(uploads), "install.sh %s" % pkg["install_arg"] if args.install
                    else ("no restart" if args.no_restart else "svc -t /service/%s" % pkg["service"])))
                continue

            # --- backup + upload ---
            for lp, rp, existed in uploads:
                if existed:
                    cb.run("cp '%s' '%s.bak-%s'" % (rp, rp, args.tag))
                cb.put(lp, rp)
                print("   uploaded %s" % rp)
            if not uploads:
                print("-- nothing to upload")

            # --- restart / install ---
            if args.install:
                o, e = cb.run("sh %s/install.sh %s" % (base, pkg["install_arg"]), timeout=60)
                print((o + e).strip())
            elif args.no_restart:
                print("-- not restarted (--no-restart)")
            elif uploads:
                cb.run("svc -t /service/%s" % pkg["service"])
                print("-- svc -t /service/%s" % pkg["service"])
            if (args.install or (uploads and not args.no_restart)):
                time.sleep(args.settle)
            verify(cb, pkg, base)
    finally:
        cb.close()
    sys.exit(rc)


def verify(cb, pkg, base):
    o, _ = cb.run("grep -m1 '^VERSION' %s/%s; svstat /service/%s 2>&1" % (
        base, pkg["version_file"], pkg["service"]))
    print("verify: " + o.strip().replace("\n", " | "))
    o, _ = cb.run("tail -n 12 /var/log/%s/current 2>/dev/null | tai64nlocal" % pkg["service"])
    print("log:\n" + "\n".join("   " + l for l in o.strip().splitlines()))
    for svc, path in pkg["verify"]:
        print("   %-42s %s" % (svc.split(".")[-1] + path, cb.dbus_get(svc, path)))


if __name__ == "__main__":
    main()
