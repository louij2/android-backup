"""android-backup: backs up Android devices over ADB, with no root and no app on the phone.

Any authorised device that is plugged in, or reachable at an ADB_TARGETS address, is backed up
once every BACKUP_INTERVAL_HOURS. Each device gets <backup dir>/<model>-<serial>/:

  files/       mirror of shared storage (/sdcard). Files deleted on the phone are kept here.
  apks/        <package>/<versionCode>/*.apk for every user-installed app
  snapshots/   <date>/ contacts.vcf, sms.xml, calls.xml, packages.txt, settings and getprop

Private app data (/data/data) can't be reached without root.

Commands:
  serve                 device watcher + status page (default)
  backup [SERIAL...]    back up connected devices now, in the foreground
  status                print the last result for each device
  adb ARGS...           run adb with this container's key (e.g. `adb devices -l`)
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import quoteattr

from . import __version__

CONFIG = Path(os.environ.get("CONFIG_DIR", "/config"))
BACKUPS = Path(os.environ.get("BACKUP_DIR", "/backups"))
INTERVAL_HOURS = float(os.environ.get("BACKUP_INTERVAL_HOURS", "24"))
RETRY_MINUTES = float(os.environ.get("RETRY_MINUTES", "60"))
BACKUP_TIMEOUT_HOURS = float(os.environ.get("BACKUP_TIMEOUT_HOURS", "12"))
TARGETS = [t.strip() for t in os.environ.get("ADB_TARGETS", "").split(",") if t.strip()]
SERIALS = {s.strip() for s in os.environ.get("SERIALS", "").split(",") if s.strip()}
EXCLUDE = re.compile(os.environ.get(
    "EXCLUDE", r"^Android/obb/|^Android/data/[^/]+/cache/|(^|/)\.thumbnails/|(^|/)\.Trash"))
KEEP_SNAPSHOTS = int(os.environ.get("KEEP_SNAPSHOTS", "30"))
PORT = int(os.environ.get("PORT", "9872"))
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
NOTIFY_SUCCESS = os.environ.get("NOTIFY_SUCCESS", "false").lower() in ("1", "true", "yes")
PUID = int(os.environ.get("PUID", "99"))  # Unraid's nobody:users, so the backups are usable over SMB
PGID = int(os.environ.get("PGID", "100"))
POLL_SECONDS = 30
CONNECT_EVERY = 300

STATE_FILE = CONFIG / "state.json"
LOG_DIR = CONFIG / "logs"
_lock = threading.Lock()  # one backup at a time
_state_lock = threading.Lock()
_seen = {"unauthorized": [], "connected": {}}  # latest poll, for the status page and metrics


def log(msg):
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# --- state ---------------------------------------------------------------------------

def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def update_state(serial, **fields):
    with _state_lock:
        st = load_state()
        st.setdefault(serial, {}).update(fields)
        CONFIG.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
        tmp.replace(STATE_FILE)


# --- adb -----------------------------------------------------------------------------

def adb(*args, timeout=120):
    return subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout, errors="surrogateescape")


def shell(adb_serial, command, timeout=300):
    """Run a device command without a pty, so stdout is clean and the exit code is real."""
    return adb("-s", adb_serial, "shell", "-T", command, timeout=timeout)


def online(adb_serial):
    try:
        return adb("-s", adb_serial, "get-state", timeout=20).stdout.strip() == "device"
    except subprocess.TimeoutExpired:
        return False


def connect_targets():
    for target in TARGETS:
        try:
            adb("connect", target, timeout=15)
        except subprocess.TimeoutExpired:
            pass


def list_devices():
    """(ready, unauthorized). ready maps the device's real serial to its best adb address, USB first."""
    try:
        lines = adb("devices", timeout=30).stdout.splitlines()[1:]
    except subprocess.TimeoutExpired:
        return {}, []
    ready, unauthorized = {}, []
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        address, status = parts[0], parts[1]
        if status == "unauthorized":
            unauthorized.append(address)
        elif status == "device":
            try:
                serial = shell(address, "getprop ro.serialno", timeout=20).stdout.strip() or address
            except subprocess.TimeoutExpired:
                continue
            is_network = ":" in address or "._tcp" in address
            if serial not in ready or not is_network:
                ready[serial] = address
    return ({s: a for s, a in ready.items() if not SERIALS or s in SERIALS}, unauthorized)


def transport_of(address):
    return "wifi" if ":" in address or "._tcp" in address else "usb"


def device_label(adb_serial, serial):
    model = shell(adb_serial, "getprop ro.product.model", timeout=20).stdout.strip() or "android"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model) + "-" + re.sub(r"[^A-Za-z0-9._-]+", "_", serial)


# --- notify --------------------------------------------------------------------------

def notify(text):
    if not WEBHOOK:
        return
    try:
        req = urllib.request.Request(WEBHOOK, data=json.dumps({"content": text}).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "android-backup"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # never let a webhook failure break a backup
        log(f"webhook failed: {e}")


# --- shared storage + APKs -----------------------------------------------------------

class Disconnected(Exception):
    pass


STAT_RE = re.compile(r"^(\d+) (\d+) /sdcard/(.+)$")


def list_files(a):
    """[(size, mtime, relative path)] of shared storage; raises if it can't be read at all."""
    r = shell(a, "find /sdcard/ -type f -exec stat -c '%s %Y %n' {} + 2>/dev/null", timeout=1800)
    out = []
    for line in r.stdout.splitlines():
        m = STAT_RE.match(line)
        if m and not EXCLUDE.search(m.group(3)):
            out.append((int(m.group(1)), int(m.group(2)), m.group(3)))
    if not out:  # before the first unlock after a reboot, shared storage is encrypted and looks empty
        raise RuntimeError("shared storage is empty or unreadable (unlock the phone once after a reboot)")
    return out


def pull(a, remote, dest, size=0):
    """Pull into a .part file so an interrupted copy never looks finished. Returns the adb error or None."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    timeout = 300 + size / 1_000_000  # 1 MB/s floor, so a wedged adbd can't hang the whole run
    try:
        r = adb("-s", a, "pull", "-a", remote, str(part), timeout=timeout)
    except subprocess.TimeoutExpired:
        part.unlink(missing_ok=True)
        return "timed out"
    if r.returncode != 0:
        part.unlink(missing_ok=True)
        return (r.stderr or r.stdout).strip()[-200:]
    part.replace(dest)
    return None


def sync_files(a, serial, top, lf):
    files = list_files(a)
    root = top / "files"
    todo = []
    for size, mtime, rel in files:
        local = root / rel
        try:
            st = local.stat()
            if st.st_size == size and abs(st.st_mtime - mtime) <= 1:
                continue
        except OSError:
            pass
        todo.append((size, rel))
    total = sum(s for s, _ in todo) or 1
    done = copied = failed = 0
    log(f"{serial}: {len(files)} files on the device, {len(todo)} to copy ({total / 1e9:.2f} GB)")
    last_pct = -1
    for size, rel in todo:
        err = pull(a, "/sdcard/" + rel, root / rel, size)
        if err:
            if not online(a):
                raise Disconnected("device went away during the file copy")
            failed += 1
            lf.write(f"FAILED {rel}: {err}\n".encode(errors="surrogateescape"))
        else:
            copied += 1
        done += size
        pct = int(done * 100 / total)
        if pct != last_pct:
            last_pct = pct
            update_state(serial, progress=pct)
    return len(files), copied, failed, done


PKG_RE = re.compile(r"^package:(\S+) versionCode:(\d+)")


def sync_apks(a, top, lf):
    copied = failed = 0
    for line in shell(a, "pm list packages -3 --show-versioncode", timeout=120).stdout.splitlines():
        m = PKG_RE.match(line.strip())
        if not m:
            continue
        pkg, version = m.groups()
        dest = top / "apks" / pkg / version
        if (dest / ".complete").exists():
            continue
        ok = True
        for path in shell(a, f"pm path {pkg}", timeout=60).stdout.split():
            if path.startswith("package:"):
                remote = path.removeprefix("package:")
                err = pull(a, remote, dest / os.path.basename(remote))
                if err:
                    if not online(a):
                        raise Disconnected("device went away during the APK copy")
                    ok = False
                    lf.write(f"FAILED {pkg}: {err}\n".encode(errors="surrogateescape"))
        if ok and dest.exists():
            (dest / ".complete").touch()
            copied += 1
        else:
            failed += 1
    return copied, failed


# --- snapshots -----------------------------------------------------------------------

def query(a, uri, columns):
    """Rows of a content provider. Free-text columns must come last: a column ends where the next begins."""
    r = shell(a, f"content query --uri {uri} --projection {':'.join(columns)}", timeout=600)
    if "Row: " not in r.stdout:
        if r.returncode != 0 or "Exception" in r.stderr + r.stdout:
            raise RuntimeError((r.stderr or r.stdout).strip()[-200:])
        return []
    rows = []
    for chunk in re.split(r"(?m)^Row: \d+ ", r.stdout)[1:]:
        row, rest = {}, chunk.rstrip("\n")
        for i, col in enumerate(columns):
            rest = rest.removeprefix(f"{col}=")
            if i + 1 < len(columns):
                value, _, rest = rest.partition(f", {columns[i + 1]}=")
                rest = f"{columns[i + 1]}={rest}"
            else:
                value = rest
            row[col] = "null" if value == "NULL" else value
        rows.append(row)
    return rows


def clean(value):
    """XML 1.0 can't carry control characters or lone surrogates from undecodable bytes."""
    return quoteattr(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]", "", value))


def write_xml(path, root, tag, rows, readable_date=True):
    """The XML format of the 'SMS Backup & Restore' app, so these can be restored with it."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n<{root} count=\"{len(rows)}\">\n")
        for row in rows:
            attrs = dict(row)
            if readable_date and attrs.get("date", "").isdigit():
                attrs["readable_date"] = dt.datetime.fromtimestamp(int(attrs["date"]) / 1000).strftime("%d %b %Y %H:%M:%S")
            f.write(f"  <{tag} " + " ".join(f"{k}={clean(v)}" for k, v in attrs.items()) + " />\n")
        f.write(f"</{root}>\n")


CONTACT_COLUMNS = ["contact_id", "mimetype", "data2", "data3", "data4", "data1"]
PHONE_TYPES = {"1": "HOME", "2": "CELL", "3": "WORK", "4": "WORK,FAX", "5": "HOME,FAX", "6": "PAGER"}
EMAIL_TYPES = {"1": "HOME", "2": "WORK"}


def vcard_text(value):
    value = "" if value == "null" else value
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def typed(prop, kinds, row):
    kind = kinds.get(row["data2"])
    return f"{prop};TYPE={kind}:{vcard_text(row['data1'])}" if kind else f"{prop}:{vcard_text(row['data1'])}"


def vcard_line(row):
    kind = row["mimetype"].rpartition("/")[2]
    if row["data1"] == "null" and kind != "organization":
        return None
    if kind == "name":
        return f"FN:{vcard_text(row['data1'])}\r\nN:{vcard_text(row['data3'])};{vcard_text(row['data2'])};;;"
    if kind == "phone_v2":
        return typed("TEL", PHONE_TYPES, row)
    if kind == "email_v2":
        return typed("EMAIL", EMAIL_TYPES, row)
    if kind == "organization":
        org = vcard_text(row["data1"])
        title = vcard_text(row["data4"])
        return "\r\n".join(([f"ORG:{org}"] if org else []) + ([f"TITLE:{title}"] if title else [])) or None
    simple = {"nickname": "NICKNAME", "note": "NOTE", "website": "URL"}
    if kind in simple:
        return f"{simple[kind]}:{vcard_text(row['data1'])}"
    if kind == "postal-address_v2":
        return f"ADR:;;{vcard_text(row['data1'])};;;;"
    return None  # photos, groups and app-specific rows don't carry over as text


def export_contacts(a, path):
    """vCards built from the raw contact rows: the provider's own vCard export needs asset-file access."""
    people = {}
    for row in query(a, "content://com.android.contacts/data", CONTACT_COLUMNS):
        line = vcard_line(row)
        if line:
            people.setdefault(row["contact_id"], []).append(line)
    with open(path, "w", encoding="utf-8", errors="replace", newline="") as f:
        for lines in people.values():
            if not any(l.startswith("FN:") for l in lines):
                lines.insert(0, "FN:\r\nN:;;;;")
            f.write("BEGIN:VCARD\r\nVERSION:3.0\r\n" + "\r\n".join(lines) + "\r\nEND:VCARD\r\n")
    return len(people)


SMS_COLUMNS = ["address", "date", "date_sent", "type", "read", "status", "locked", "protocol", "service_center",
               "subject", "body"]
CALL_COLUMNS = ["number", "duration", "date", "type", "presentation", "name"]


def snapshot(a, top, lf):
    """Dated copies, so a phone that lost its messages can't overwrite the last good copy."""
    snap = top / "snapshots" / f"{dt.date.today():%Y-%m-%d}"
    snap.mkdir(parents=True, exist_ok=True)
    counts, problems = {}, []

    def attempt(name, func):
        try:
            counts[name] = func()
        except (RuntimeError, subprocess.SubprocessError, OSError) as e:
            problems.append(name)
            lf.write(f"FAILED {name}: {e}\n".encode(errors="surrogateescape"))

    def sms():
        rows = query(a, "content://sms", SMS_COLUMNS)
        write_xml(snap / "sms.xml", "smses", "sms", rows)
        return len(rows)

    def calls():
        rows = query(a, "content://call_log/calls", CALL_COLUMNS)
        for r in rows:
            r["contact_name"] = r.pop("name")
        write_xml(snap / "calls.xml", "calls", "call", rows)
        return len(rows)

    attempt("contacts", lambda: export_contacts(a, snap / "contacts.vcf"))
    attempt("sms", sms)
    attempt("calls", calls)
    for name, command in (("packages.txt", "pm list packages -f --show-versioncode"), ("getprop.txt", "getprop"),
                          ("settings-global.txt", "settings list global"), ("settings-secure.txt", "settings list secure"),
                          ("settings-system.txt", "settings list system")):
        try:
            (snap / name).write_bytes(shell(a, command, timeout=120).stdout.encode(errors="surrogateescape"))
        except subprocess.SubprocessError as e:
            problems.append(name)
            lf.write(f"FAILED {name}: {e}\n".encode())
    for old in sorted((top / "snapshots").iterdir())[:-KEEP_SNAPSHOTS]:
        shutil.rmtree(old, ignore_errors=True)
    return counts, problems


# --- backup --------------------------------------------------------------------------

def fix_ownership(top):
    """adb runs as root; hand the backup to PUID:PGID. Group-only: it holds messages and contacts."""
    for root, dirs, files in os.walk(top):
        for name, mode in [(n, 0o770) for n in dirs] + [(n, 0o660) for n in files]:
            path = os.path.join(root, name)
            try:
                os.chown(path, PUID, PGID)
                os.chmod(path, mode)
            except OSError as e:
                log(f"could not fix ownership of {path}: {e}")
    os.chown(top, PUID, PGID)
    os.chmod(top, 0o770)


def backup_size(top):
    total = 0
    for root, _dirs, files in os.walk(top):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def backup_one(serial, a):
    """One backup of one device. Returns True on success."""
    label = device_label(a, serial)
    top = BACKUPS / label
    top.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / f"{label}-{dt.datetime.now():%Y%m%d-%H%M%S}.log"
    started = time.time()
    transport = transport_of(a)
    update_state(serial, name=label, running=True, last_attempt=started, transport=transport, progress=0)
    log(f"{label}: backup starting over {transport}")
    gone = False
    try:
        with open(logfile, "wb") as lf:
            n_files, copied, failed, copied_bytes = sync_files(a, serial, top, lf)
            apks, apk_failed = sync_apks(a, top, lf)
            counts, problems = snapshot(a, top, lf)
        parts = [f"{n_files} files ({copied} new, {copied_bytes / 1e9:.2f} GB)", f"{apks} new APKs"]
        parts += [f"{v} {k}" for k, v in counts.items()]
        if failed or apk_failed:
            parts.append(f"{failed} files + {apk_failed} APKs could not be read")
        if problems:
            parts.append("no " + ", ".join(problems))
        message, ok = "; ".join(parts), True
        fields = dict(last_success=time.time(), files=n_files, files_failed=failed, apks_failed=apk_failed,
                      copied_bytes=copied_bytes, snapshot_problems=len(problems), progress=100)
    except Disconnected as e:
        message, ok, gone, fields = str(e), False, True, {}
    except (RuntimeError, subprocess.SubprocessError, OSError) as e:
        message, ok, fields = str(e)[-300:], False, {}
    fix_ownership(top)
    duration = time.time() - started
    update_state(serial, running=False, last_rc=0 if ok else 1, last_duration=round(duration), last_message=message,
                 last_log=str(logfile), size_bytes=backup_size(top), **fields)
    log(f"{label}: {'OK' if ok else 'FAILED'} in {duration:.0f}s: {message}")
    for old in sorted(LOG_DIR.glob(f"{label}-*.log"))[:-30]:  # keep the last 30 logs per device
        old.unlink(missing_ok=True)
    if ok and NOTIFY_SUCCESS:
        notify(f"✅ Android backup OK: **{label}**: {message}")
    elif not ok and not gone:  # unplugging mid-run isn't a failure; it resumes next time
        notify(f"⚠️ Android backup FAILED: **{label}**: {message}")
    return ok


def due(serial):
    s = load_state().get(serial, {})
    now = time.time()
    return (now - s.get("last_success", 0) >= INTERVAL_HOURS * 3600
            and now - s.get("last_attempt", 0) >= RETRY_MINUTES * 60)


def watcher():
    """Back up whichever devices are present and due; a plugged-in device is picked up within a poll."""
    last_connect = 0
    while True:
        if TARGETS and time.time() - last_connect >= CONNECT_EVERY:
            connect_targets()
            last_connect = time.time()
        ready, unauthorized = list_devices()
        _seen.update(unauthorized=unauthorized, connected={s: transport_of(a) for s, a in ready.items()})
        for serial, a in ready.items():
            if due(serial):
                with _lock:
                    backup_one(serial, a)
        time.sleep(POLL_SECONDS)


def run_now(serials=None):
    ready, _ = list_devices()
    results = {}
    for serial, a in ready.items():
        if not serials or serial in serials:
            with _lock:
                results[serial] = backup_one(serial, a)
    return results


# --- web -----------------------------------------------------------------------------

def label_escape(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def metrics():
    st = load_state()
    lines = ["# HELP android_backup_info Exporter info", "# TYPE android_backup_info gauge",
             f'android_backup_info{{version="{__version__}"}} 1',
             "# TYPE android_backup_unauthorized_devices gauge",
             f"android_backup_unauthorized_devices {len(_seen['unauthorized'])}"]
    series = {
        "last_success_timestamp_seconds": "last_success",
        "last_attempt_timestamp_seconds": "last_attempt",
        "last_duration_seconds": "last_duration",
        "size_bytes": "size_bytes",
        "files": "files",
        "files_failed": "files_failed",
        "apks_failed": "apks_failed",
        "snapshot_problems": "snapshot_problems",
    }
    for metric, key in series.items():
        lines.append(f"# TYPE android_backup_{metric} gauge")
        for serial, s in st.items():
            if key in s:
                lines.append(f'android_backup_{metric}{{serial="{label_escape(serial)}",name="{label_escape(s.get("name", serial))}"}} {s[key]}')
    for name in ("last_ok", "running", "progress_percent", "connected", "safe_to_unplug"):
        lines.append(f"# TYPE android_backup_{name} gauge")
    for serial, s in st.items():
        lab = f'serial="{label_escape(serial)}",name="{label_escape(s.get("name", serial))}"'
        running = bool(s.get("running"))
        if "last_rc" in s:
            lines.append(f"android_backup_last_ok{{{lab}}} {1 if s['last_rc'] == 0 else 0}")
        lines.append(f"android_backup_running{{{lab}}} {1 if running else 0}")
        lines.append(f"android_backup_progress_percent{{{lab}}} {s.get('progress', 0) if running else 0}")
        lines.append(f"android_backup_connected{{{lab}}} {1 if serial in _seen['connected'] else 0}")
        lines.append(f"android_backup_safe_to_unplug{{{lab}}} {0 if running and s.get('transport') == 'usb' else 1}")
    return "\n".join(lines) + "\n"


def fmt_ts(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "never"


def status_page():
    st = load_state()
    rows = []
    for serial in sorted(set(st) | set(_seen["connected"])):
        s = st.get(serial, {})
        badge = f"running {s.get('progress', 0)}%" if s.get("running") else (
            "ok" if s.get("last_rc") == 0 else ("failed" if "last_rc" in s else "pending"))
        link = _seen["connected"].get(serial)
        q = urllib.parse.quote(serial, safe="")
        rows.append(
            f"<tr><td>{html.escape(s.get('name', serial))}<br><small>{html.escape(serial)}</small></td>"
            f"<td class={badge.split()[0]}>{badge}</td><td>{link or 'away'}</td><td>{fmt_ts(s.get('last_success'))}</td>"
            f"<td>{s.get('size_bytes', 0) / 1e9:.1f} GB</td>"
            f"<td><small>{html.escape(s.get('last_message', ''))}</small></td>"
            f"<td><form method=post action='/api/backup?serial={q}'><button>Back up now</button></form></td></tr>")
    waiting = "".join(f"<p class=failed>⚠ <b>{html.escape(a)}</b>: tap <b>Allow</b> on the device (tick “Always allow”).</p>"
                      for a in _seen["unauthorized"])
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>android-backup</title><style>
body{{font:14px system-ui,sans-serif;margin:16px;color:#222;background:#fafafa}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:8px;text-align:left;vertical-align:top}}
.ok{{color:#15803d}}.failed{{color:#b91c1c}}.running{{color:#1d4ed8}}.pending{{color:#666}}
@media (prefers-color-scheme:dark){{body{{background:#111;color:#ddd}}td,th{{border-color:#333}}}}
</style><h2>android-backup {__version__}</h2>
<p>Every {INTERVAL_HOURS:g} h per device, whenever it's connected &middot; backups in <code>{BACKUPS}</code></p>{waiting}
<table><tr><th>Device</th><th>Status</th><th>Link</th><th>Last success</th><th>Size</th><th>Last result</th><th></th></tr>
{''.join(rows) or '<tr><td colspan=7>No devices yet. Turn on USB debugging and plug one in.</td></tr>'}
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
        serial = urllib.parse.unquote(self.path.partition("serial=")[2]) or None
        if _lock.locked():
            return self._send(409, "a backup is already running", "text/plain")
        threading.Thread(target=run_now, args=([serial] if serial else None,), daemon=True).start()
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, *_):
        pass


# --- commands ------------------------------------------------------------------------

def cmd_serve(_args):
    log(f"android-backup {__version__}: every {INTERVAL_HOURS:g} h; targets {TARGETS or 'USB only'}")
    with _state_lock:  # a restart mid-backup must not leave a device showing as running
        st = load_state()
        for s in st.values():
            s["running"] = False
        if st:
            STATE_FILE.write_text(json.dumps(st, indent=1, sort_keys=True))
    adb("start-server", timeout=30)
    threading.Thread(target=watcher, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


def cmd_backup(args):
    connect_targets()
    results = run_now(args.serial or None)
    if not results:
        print("no authorised device connected")
    return 0 if results and all(results.values()) else 1


def cmd_status(_args):
    for serial, s in sorted(load_state().items()):
        print(f"{s.get('name', serial)}: rc={s.get('last_rc')} last_success={fmt_ts(s.get('last_success'))} "
              f"size={s.get('size_bytes', 0) / 1e9:.1f}GB via {s.get('transport', '-')}: {s.get('last_message', '')}")
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "adb":
        return subprocess.run(["adb", *sys.argv[2:]]).returncode
    p = argparse.ArgumentParser(prog="android-backup", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve")
    b = sub.add_parser("backup")
    b.add_argument("serial", nargs="*")
    sub.add_parser("status")
    args = p.parse_args()
    return {"serve": cmd_serve, "backup": cmd_backup, "status": cmd_status, None: cmd_serve}[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
