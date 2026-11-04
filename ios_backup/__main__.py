"""ios-backup: scheduled iPhone/iPad backups in Finder/iTunes (MobileBackup2) format.

Backs up every paired device found in /var/lib/lockdown, over USB when the device is
plugged in and over Wi-Fi (Bonjour "mobdev2") otherwise. The output is a standard
<backup dir>/<UDID>/ folder that Finder, iTunes and iMazing can open.

Commands:
  serve                 scheduler + status page (default)
  pair                  pair a USB-connected device and turn on Wi-Fi connections
  backup [UDID...]      back up now, in the foreground
  status                print the last result for each device
  pmd3 ARGS...          run pymobiledevice3 directly (e.g. `pmd3 backup2 encryption`)
"""
import argparse
import datetime as dt
import html
import json
import os
import plistlib
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__

LOCKDOWN = Path("/var/lib/lockdown")
CONFIG = Path(os.environ.get("CONFIG_DIR", "/config"))
BACKUPS = Path(os.environ.get("BACKUP_DIR", "/backups"))
TIMES = [t.strip() for t in os.environ.get("BACKUP_TIMES", "03:00").split(",") if t.strip()]
RETRY_MINUTES = float(os.environ.get("RETRY_MINUTES", "20"))
RETRY_WINDOW_HOURS = float(os.environ.get("RETRY_WINDOW_HOURS", "5"))
BACKUP_TIMEOUT_HOURS = float(os.environ.get("BACKUP_TIMEOUT_HOURS", "8"))
PORT = int(os.environ.get("PORT", "9871"))
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
NOTIFY_SUCCESS = os.environ.get("NOTIFY_SUCCESS", "false").lower() in ("1", "true", "yes")
RUN_ON_START = os.environ.get("RUN_ON_START", "false").lower() in ("1", "true", "yes")
USBMUXD = os.environ.get("USBMUXD", "true").lower() in ("1", "true", "yes")
PMD3 = [sys.executable, "-m", "pymobiledevice3"]

STATE_FILE = CONFIG / "state.json"
LOG_DIR = CONFIG / "logs"
_lock = threading.Lock()  # one backup at a time: the phone only serves one MobileBackup2 session
_state_lock = threading.Lock()


def log(msg):
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# --- state ---------------------------------------------------------------------------

def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def update_state(udid, **fields):
    with _state_lock:
        st = load_state()
        st.setdefault(udid, {}).update(fields)
        CONFIG.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
        tmp.replace(STATE_FILE)


# --- devices -------------------------------------------------------------------------

def paired_udids():
    """Every device with a pair record. Optional UDIDS env narrows the list."""
    wanted = {u.strip() for u in os.environ.get("UDIDS", "").split(",") if u.strip()}
    found = sorted(p.stem for p in LOCKDOWN.glob("*.plist") if p.stem != "SystemConfiguration")
    return [u for u in found if not wanted or u in wanted]


def usb_udids():
    try:
        out = subprocess.run(PMD3 + ["usbmux", "list"], capture_output=True, text=True, timeout=60).stdout
        devs = json.loads(out[out.index("["):]) if "[" in out else []
    except (subprocess.SubprocessError, ValueError):
        return set()
    return {d.get("UniqueDeviceID") for d in devs if d.get("ConnectionType") == "USB"}


def backup_size(udid):
    total = 0
    for root, _dirs, files in os.walk(BACKUPS / udid):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def device_name(udid):
    """Name from the backup's own Info.plist once one exists."""
    try:
        with open(BACKUPS / udid / "Info.plist", "rb") as f:
            return plistlib.load(f).get("Device Name") or udid
    except (OSError, plistlib.InvalidFileException):
        return udid


# --- notify --------------------------------------------------------------------------

def notify(text):
    if not WEBHOOK:
        return
    try:
        req = urllib.request.Request(WEBHOOK, data=json.dumps({"content": text}).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "ios-backup"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # never let a webhook failure break a backup
        log(f"webhook failed: {e}")


# --- backup --------------------------------------------------------------------------

UNREACHABLE = ("not found", "no device", "devicenotfound", "connectionfailed", "connection refused",
               "timed out", "no route", "not connected", "nodeviceconnected")


def backup_one(udid, usb):
    """Run one MobileBackup2 backup. Returns (ok, reachable, message)."""
    transport = "usb" if usb else "wifi"
    cmd = PMD3 + ["backup2", "backup", str(BACKUPS), "--udid", udid] + ([] if usb else ["--mobdev2"])
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / f"{udid}-{dt.datetime.now():%Y%m%d-%H%M%S}.log"
    started = time.time()
    update_state(udid, running=True, last_attempt=started, transport=transport)
    log(f"{udid}: backup starting over {transport}")
    try:
        with open(logfile, "w") as lf:
            rc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                timeout=BACKUP_TIMEOUT_HOURS * 3600).returncode
    except subprocess.TimeoutExpired:
        rc = -1
    duration = time.time() - started
    tail = logfile.read_text(errors="replace").replace("\r", "\n").strip().splitlines()[-3:]
    message = " | ".join(tail)[-400:]
    ok = rc == 0
    reachable = ok or not any(s in message.lower() for s in UNREACHABLE)
    fields = dict(running=False, last_rc=rc, last_duration=round(duration), last_message=message,
                  last_log=str(logfile))
    if ok:
        fields.update(last_success=time.time(), size_bytes=backup_size(udid), name=device_name(udid))
    update_state(udid, **fields)
    log(f"{udid}: {'OK' if ok else 'FAILED'} rc={rc} in {duration:.0f}s ({transport})")
    for old in sorted(LOG_DIR.glob(f"{udid}-*.log"))[:-30]:  # keep the last 30 logs per device
        old.unlink(missing_ok=True)
    return ok, reachable, message


def run_round(udids=None, retry=True):
    """Back up each device once; retry unreachable ones until the window closes."""
    pending = list(udids or paired_udids())
    deadline = time.time() + (RETRY_WINDOW_HOURS * 3600 if retry else 0)
    while pending:
        with _lock:
            usb = usb_udids()
            still = []
            for udid in pending:
                ok, reachable, msg = backup_one(udid, udid in usb)
                name = device_name(udid)
                if ok:
                    if NOTIFY_SUCCESS:
                        size = load_state().get(udid, {}).get("size_bytes", 0)
                        notify(f"✅ iOS backup OK: **{name}** ({size / 1e9:.1f} GB)")
                elif reachable or time.time() >= deadline:
                    notify(f"⚠️ iOS backup FAILED: **{name}**: {msg[-300:]}")
                else:
                    still.append(udid)
            pending = still
        if pending:
            if time.time() >= deadline:
                break
            log(f"not reachable yet: {', '.join(pending)}; retrying in {RETRY_MINUTES:g} min")
            time.sleep(RETRY_MINUTES * 60)


def next_run(now):
    candidates = []
    for t in TIMES:
        h, m = (int(x) for x in t.split(":"))
        c = now.replace(hour=h, minute=m, second=0, microsecond=0)
        candidates.append(c if c > now else c + dt.timedelta(days=1))
    return min(candidates)


def scheduler():
    if RUN_ON_START:
        run_round()
    while True:
        nxt = next_run(dt.datetime.now())
        log(f"next backup round at {nxt:%Y-%m-%d %H:%M}")
        while dt.datetime.now() < nxt:
            time.sleep(min(60, max(1, (nxt - dt.datetime.now()).total_seconds())))
        run_round()


# --- web -----------------------------------------------------------------------------

def metrics():
    st = load_state()
    lines = ["# HELP ios_backup_info Exporter info", "# TYPE ios_backup_info gauge",
             f'ios_backup_info{{version="{__version__}"}} 1']
    series = {
        "last_success_timestamp_seconds": "last_success",
        "last_attempt_timestamp_seconds": "last_attempt",
        "last_duration_seconds": "last_duration",
        "size_bytes": "size_bytes",
    }
    for metric, key in series.items():
        lines.append(f"# TYPE ios_backup_{metric} gauge")
        for udid, s in st.items():
            if key in s:
                lines.append(f'ios_backup_{metric}{{udid="{udid}",name="{s.get("name", udid)}"}} {s[key]}')
    lines.append("# TYPE ios_backup_last_ok gauge")
    lines.append("# TYPE ios_backup_running gauge")
    for udid, s in st.items():
        lab = f'udid="{udid}",name="{s.get("name", udid)}"'
        if "last_rc" in s:
            lines.append(f"ios_backup_last_ok{{{lab}}} {1 if s['last_rc'] == 0 else 0}")
        lines.append(f"ios_backup_running{{{lab}}} {1 if s.get('running') else 0}")
    return "\n".join(lines) + "\n"


def fmt_ts(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "never"


def status_page():
    st = load_state()
    rows = []
    for udid in sorted(set(paired_udids()) | set(st)):
        s = st.get(udid, {})
        ok = s.get("last_rc") == 0
        badge = "running" if s.get("running") else ("ok" if ok else ("failed" if "last_rc" in s else "pending"))
        rows.append(
            f"<tr><td>{html.escape(s.get('name', udid))}<br><small>{udid}</small></td>"
            f"<td class={badge}>{badge}</td><td>{fmt_ts(s.get('last_success'))}</td>"
            f"<td>{fmt_ts(s.get('last_attempt'))} ({s.get('transport', '-')})</td>"
            f"<td>{s.get('size_bytes', 0) / 1e9:.1f} GB</td>"
            f"<td><small>{html.escape(s.get('last_message', ''))}</small></td>"
            f"<td><form method=post action='/api/backup?udid={udid}'><button>Back up now</button></form></td></tr>")
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>ios-backup</title><style>
body{{font:14px system-ui,sans-serif;margin:16px;color:#222;background:#fafafa}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:8px;text-align:left;vertical-align:top}}
.ok{{color:#15803d}}.failed{{color:#b91c1c}}.running{{color:#1d4ed8}}.pending{{color:#666}}
@media (prefers-color-scheme:dark){{body{{background:#111;color:#ddd}}td,th{{border-color:#333}}}}
</style><h2>ios-backup {__version__}</h2>
<p>Schedule: {", ".join(TIMES)} &middot; backups in <code>{BACKUPS}</code> &middot;
next round {next_run(dt.datetime.now()):%Y-%m-%d %H:%M}</p>
<table><tr><th>Device</th><th>Status</th><th>Last success</th><th>Last attempt</th><th>Size</th><th>Last message</th><th></th></tr>
{''.join(rows) or '<tr><td colspan=7>No paired devices. Plug one in and run <code>ios-backup pair</code>.</td></tr>'}
</table>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/metrics"):
            self._send(200, metrics(), "text/plain; version=0.0.4")
        elif self.path.startswith("/api/status"):
            self._send(200, json.dumps(load_state(), indent=1), "application/json")
        elif self.path == "/":
            self._send(200, status_page())
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if not self.path.startswith("/api/backup"):
            return self._send(404, "not found", "text/plain")
        udid = self.path.partition("udid=")[2] or None
        if udid and udid not in paired_udids():
            return self._send(400, "unknown udid", "text/plain")
        if _lock.locked():
            return self._send(409, "a backup is already running", "text/plain")
        threading.Thread(target=run_round, args=([udid] if udid else None, False), daemon=True).start()
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, *_):
        pass


# --- commands ------------------------------------------------------------------------

def start_usbmuxd():
    if not USBMUXD or not Path("/dev/bus/usb").exists():
        log("usbmuxd not started (no /dev/bus/usb): Wi-Fi only")
        return
    LOCKDOWN.mkdir(parents=True, exist_ok=True)
    # -U root: keep root so it can read pair records in a 0700 lockdown dir.
    subprocess.Popen(["usbmuxd", "-f", "-U", "root"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        if Path("/var/run/usbmuxd").exists():
            log("usbmuxd started")
            return
        time.sleep(0.5)
    log("usbmuxd did not come up: USB unavailable, Wi-Fi only")


def cmd_serve(_args):
    log(f"ios-backup {__version__}: devices {paired_udids() or 'none paired yet'}; schedule {TIMES}")
    start_usbmuxd()
    threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


def cmd_pair(_args):
    if not Path("/var/run/usbmuxd").exists():
        start_usbmuxd()
    print("Unlock the device and tap Trust, then enter its passcode.")
    for _ in range(36):
        r = subprocess.run(PMD3 + ["lockdown", "pair"], capture_output=True, text=True)
        out = r.stdout + r.stderr
        if r.returncode == 0 or "SavePairRecord" in out:
            break
        if not any(s in out for s in ("Pending", "PasswordProtected", "Dialog")):
            print(out.strip()[-500:])
            return 1
        time.sleep(5)
    subprocess.run(PMD3 + ["lockdown", "wifi-connections", "on"])
    print("Paired. Wi-Fi connections enabled. Device list:", paired_udids())
    return 0


def cmd_backup(args):
    if not Path("/var/run/usbmuxd").exists():
        start_usbmuxd()
    run_round(args.udid or None, retry=False)
    st = load_state()
    return 0 if all(st.get(u, {}).get("last_rc") == 0 for u in (args.udid or paired_udids())) else 1


def cmd_status(_args):
    for udid, s in sorted(load_state().items()):
        print(f"{s.get('name', udid)} ({udid}): rc={s.get('last_rc')} last_success={fmt_ts(s.get('last_success'))} "
              f"size={s.get('size_bytes', 0) / 1e9:.1f}GB via {s.get('transport', '-')}")
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "pmd3":
        return subprocess.run(PMD3 + sys.argv[2:]).returncode
    p = argparse.ArgumentParser(prog="ios-backup", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve")
    sub.add_parser("pair")
    b = sub.add_parser("backup")
    b.add_argument("udid", nargs="*")
    sub.add_parser("status")
    args = p.parse_args()
    return {"serve": cmd_serve, "pair": cmd_pair, "backup": cmd_backup, "status": cmd_status,
            None: cmd_serve}[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
