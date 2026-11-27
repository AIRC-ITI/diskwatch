#!/usr/bin/env python3
"""diskwatch server: receives agent reports, stores them, serves a dashboard, sends alerts.

Usage: server.py [--config config.json]
Standard library only.
"""
import argparse
import html
import json
import smtplib
import sqlite3
import threading
import time
import urllib.request
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULTS = {
    "listen_host": "0.0.0.0",
    "listen_port": 8080,
    "token": "change-me",
    "db_path": "diskwatch.db",
    "stale_minutes": 90,          # host is STALE if no report for this long
    "history_days": 365,
    "realert_hours": 24,          # repeat alerts for unresolved problems
    "thresholds": {
        "temp_warn": 60, "temp_crit": 70,
        "reallocated_warn": 1, "reallocated_crit": 50,
        "wear_warn": 80, "wear_crit": 90,
    },
    "slack_webhook": "",
    "telegram": {"bot_token": "", "chat_id": ""},
    "smtp": {"host": "", "port": 587, "user": "", "password": "", "starttls": True,
             "from": "diskwatch@localhost", "to": []},
}

RANK = {"OK": 0, "WARN": 1, "STALE": 2, "CRIT": 3}


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULTS))
    if path:
        with open(path) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


# ---------- health evaluation ----------

def evaluate_disk(d, t):
    """Return (status, [reasons]) for one disk record."""
    if d.get("error"):
        return "WARN", [f"could not read SMART data: {d['error']}"]
    status, reasons = "OK", []

    def bump(level, msg):
        nonlocal status
        reasons.append(msg)
        if RANK[level] > RANK[status]:
            status = level

    if d.get("smart_passed") is False:
        bump("CRIT", "SMART overall health check FAILED")
    if d.get("nvme_critical_warning"):
        bump("CRIT", f"NVMe critical warning flags: {d['nvme_critical_warning']}")
    spare, thr = d.get("nvme_available_spare"), d.get("nvme_spare_threshold")
    if spare is not None and thr is not None and spare <= thr:
        bump("CRIT", f"NVMe spare {spare}% at/below threshold {thr}%")
    if d.get("nvme_media_errors"):
        bump("WARN", f"NVMe media errors: {d['nvme_media_errors']}")

    wear = d.get("nvme_percentage_used")
    if wear is None:
        wear = d.get("ssd_wear_used_pct")
    if wear is not None:
        if wear >= t["wear_crit"]:
            bump("CRIT", f"wear {wear}% used")
        elif wear >= t["wear_warn"]:
            bump("WARN", f"wear {wear}% used")

    re = d.get("reallocated_sectors")
    if re is not None and re >= t["reallocated_warn"]:
        bump("CRIT" if re >= t["reallocated_crit"] else "WARN", f"{re} reallocated sectors")
    for key, label in (("pending_sectors", "pending sectors"),
                       ("offline_uncorrectable", "offline uncorrectable sectors")):
        if d.get(key):
            bump("WARN", f"{d[key]} {label}")

    temp = d.get("temperature_c")
    if temp is not None:
        if temp >= t["temp_crit"]:
            bump("CRIT", f"temperature {temp}°C")
        elif temp >= t["temp_warn"]:
            bump("WARN", f"temperature {temp}°C")
    return status, reasons


def evaluate_host(report, t):
    host_status, problems = "OK", []
    for d in report.get("disks", []):
        s, r = evaluate_disk(d, t)
        d["_status"], d["_reasons"] = s, r
        if RANK[s] > RANK[host_status]:
            host_status = s
        problems += [f"{d.get('device')} ({d.get('model') or '?'}): {x}" for x in r]
    if not report.get("disks"):
        host_status, problems = "WARN", ["no disks reported (smartctl found nothing?)"]
    return host_status, problems


# ---------- storage ----------

class Store:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS hosts(
            hostname TEXT PRIMARY KEY, last_seen INTEGER, status TEXT,
            problems TEXT, report TEXT, last_alert_status TEXT, last_alert_at INTEGER);
        CREATE TABLE IF NOT EXISTS readings(
            hostname TEXT, device TEXT, ts INTEGER, status TEXT, data TEXT);
        CREATE INDEX IF NOT EXISTS idx_readings ON readings(hostname, device, ts);
        """)

    def save(self, host, status, problems, report, now, keep_days):
        with self.lock, self.db:
            self.db.execute(
                """INSERT INTO hosts(hostname,last_seen,status,problems,report) VALUES(?,?,?,?,?)
                   ON CONFLICT(hostname) DO UPDATE SET last_seen=excluded.last_seen,
                   status=excluded.status, problems=excluded.problems, report=excluded.report""",
                (host, now, status, json.dumps(problems), json.dumps(report)))
            for d in report.get("disks", []):
                self.db.execute("INSERT INTO readings VALUES(?,?,?,?,?)",
                                (host, d.get("device"), now, d.get("_status"), json.dumps(d)))
            self.db.execute("DELETE FROM readings WHERE ts < ?", (now - keep_days * 86400,))

    def hosts(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT hostname,last_seen,status,problems,report,last_alert_status,last_alert_at "
                "FROM hosts ORDER BY hostname").fetchall()
        return [dict(hostname=r[0], last_seen=r[1], status=r[2], problems=json.loads(r[3]),
                     report=json.loads(r[4]), last_alert_status=r[5], last_alert_at=r[6]) for r in rows]

    def mark_alerted(self, host, status, now):
        with self.lock, self.db:
            self.db.execute("UPDATE hosts SET last_alert_status=?, last_alert_at=? WHERE hostname=?",
                            (status, now, host))


# ---------- alerts ----------

def send_alert(cfg, subject, body):
    if cfg["slack_webhook"]:
        try:
            req = urllib.request.Request(
                cfg["slack_webhook"],
                data=json.dumps({"text": f"*{subject}*\n{body}"}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            print(f"slack alert failed: {e}")
    telegram = cfg["telegram"]
    if telegram["bot_token"] and telegram["chat_id"]:
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{telegram['bot_token']}/sendMessage",
                data=json.dumps({"chat_id": telegram["chat_id"],
                                 "text": f"{subject}\n{body}"}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            print(f"Telegram alert failed: {e}")
    s = cfg["smtp"]
    if s["host"] and s["to"]:
        try:
            m = EmailMessage()
            m["Subject"], m["From"], m["To"] = subject, s["from"], ", ".join(s["to"])
            m.set_content(body)
            with smtplib.SMTP(s["host"], s["port"], timeout=15) as smtp:
                if s["starttls"]:
                    smtp.starttls()
                if s["user"]:
                    smtp.login(s["user"], s["password"])
                smtp.send_message(m)
        except Exception as e:
            print(f"email alert failed: {e}")
    print(f"ALERT: {subject}\n{body}")


def maybe_alert(cfg, store, host, status, problems, last_status, last_at, now):
    """Alert on status change, and re-alert on unresolved problems every realert_hours."""
    last_status = last_status or "OK"
    changed = status != last_status
    repeat = status != "OK" and last_at and now - last_at > cfg["realert_hours"] * 3600
    if not (changed or repeat):
        return
    if status == "OK":
        if last_status == "OK":
            return
        send_alert(cfg, f"[diskwatch] RECOVERED: {host}", f"{host} is healthy again.")
    else:
        body = "\n".join(f"- {p}" for p in problems) or "(no details)"
        send_alert(cfg, f"[diskwatch] {status}: {host}", body)
    store.mark_alerted(host, status, now)


def stale_loop(cfg, store):
    while True:
        time.sleep(60)
        now = int(time.time())
        for h in store.hosts():
            if now - h["last_seen"] > cfg["stale_minutes"] * 60 and h["status"] != "STALE":
                mins = (now - h["last_seen"]) // 60
                with store.lock, store.db:
                    store.db.execute("UPDATE hosts SET status='STALE' WHERE hostname=?", (h["hostname"],))
                maybe_alert(cfg, store, h["hostname"], "STALE",
                            [f"no report for {mins} minutes"], h["last_alert_status"], h["last_alert_at"], now)


# ---------- dashboard ----------

def fmt_bytes(n):
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1000:
            return f"{n:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} EB"


def ago(secs):
    if secs < 90:
        return f"{secs}s ago"
    if secs < 5400:
        return f"{secs // 60}m ago"
    if secs < 172800:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def render(hosts, now):
    e = html.escape
    counts = {k: 0 for k in RANK}
    for h in hosts:
        counts[h["status"]] += 1
    rows = []
    for h in sorted(hosts, key=lambda x: (-RANK[x["status"]], x["hostname"])):
        disk_rows = []
        for d in h["report"].get("disks", []):
            st = d.get("_status", "OK")
            wear = d.get("nvme_percentage_used", d.get("ssd_wear_used_pct"))
            kind = "NVMe" if "nvme_percentage_used" in d else ("HDD" if d.get("rotation_rate") else "SSD")
            temp = "" if d.get("temperature_c") is None else "%s°C" % d["temperature_c"]
            poh = "" if d.get("power_on_hours") is None else "{:,} h".format(d["power_on_hours"])
            wear_s = "" if wear is None else "%s%%" % wear
            disk_rows.append(
                f"<tr><td>{e(str(d.get('device')))}</td><td>{kind}</td>"
                f"<td>{e(str(d.get('model') or '?'))}</td><td>{fmt_bytes(d.get('capacity_bytes'))}</td>"
                f"<td>{temp}</td><td>{poh}</td><td>{wear_s}</td>"
                f"<td>{e(str(d.get('reallocated_sectors', '')))}</td>"
                f"<td><span class='b {st}'>{st}</span> {e('; '.join(d.get('_reasons', [])))}</td></tr>")
        age = now - h["last_seen"]
        rows.append(
            f"<details {'open' if h['status'] != 'OK' else ''}><summary>"
            f"<span class='b {h['status']}'>{h['status']}</span> <b>{e(h['hostname'])}</b>"
            f"<span class='m'>{len(h['report'].get('disks', []))} disks · last report {ago(age)}</span></summary>"
            f"<table><tr><th>Device</th><th>Type</th><th>Model</th><th>Size</th><th>Temp</th>"
            f"<th>Power-on</th><th>Wear</th><th>Realloc</th><th>Status</th></tr>{''.join(disk_rows)}</table></details>")
    summary = " ".join(f"<span class='b {k}'>{counts[k]} {k}</span>" for k in ("CRIT", "STALE", "WARN", "OK") if counts[k])
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Disk Health</title><style>
body{{font:14px system-ui,sans-serif;margin:0;padding:24px;background:#f6f7f9;color:#1a1d21}}
h1{{font-size:20px;margin:0 0 12px}} .sum{{margin-bottom:16px}}
details{{background:#fff;border:1px solid #dde1e6;border-radius:8px;margin-bottom:8px;padding:10px 14px}}
summary{{cursor:pointer}} .m{{color:#6b7280;margin-left:12px;font-size:12px}}
table{{border-collapse:collapse;width:100%;margin-top:10px;font-size:13px}}
th,td{{text-align:left;padding:5px 8px;border-top:1px solid #eef0f3}} th{{color:#6b7280;font-weight:500}}
.b{{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600;margin-right:6px}}
.OK{{background:#d9f2e3;color:#0d6b35}} .WARN{{background:#fff0c2;color:#7a5800}}
.CRIT{{background:#fbd5d5;color:#a11}} .STALE{{background:#e3e5e8;color:#444}}
@media(prefers-color-scheme:dark){{body{{background:#14161a;color:#e6e8eb}}details{{background:#1d2026;border-color:#2c3038}}
th,td{{border-color:#2c3038}}}}
</style></head><body><h1>Disk Health</h1><div class="sum">{summary or 'No hosts have reported yet.'}</div>
{''.join(rows)}</body></html>"""


# ---------- HTTP ----------

def make_handler(cfg, store):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            now = int(time.time())
            if self.path == "/":
                self._send(200, render(store.hosts(), now))
            elif self.path == "/api/status":
                out = [{"hostname": h["hostname"], "status": h["status"], "last_seen": h["last_seen"],
                        "problems": h["problems"]} for h in store.hosts()]
                self._send(200, json.dumps(out, indent=2), "application/json")
            elif self.path == "/healthz":
                self._send(200, "ok", "text/plain")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            if self.path != "/api/report":
                return self._send(404, "not found", "text/plain")
            if self.headers.get("X-Diskwatch-Token") != cfg["token"]:
                return self._send(401, "bad token", "text/plain")
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n > 2_000_000:
                    return self._send(413, "too large", "text/plain")
                report = json.loads(self.rfile.read(n))
                host = str(report["hostname"])[:255]
                assert isinstance(report.get("disks"), list)
            except Exception:
                return self._send(400, "invalid report", "text/plain")
            now = int(time.time())
            status, problems = evaluate_host(report, cfg["thresholds"])
            prev = next((h for h in store.hosts() if h["hostname"] == host), None)
            store.save(host, status, problems, report, now, cfg["history_days"])
            maybe_alert(cfg, store, host, status, problems,
                        prev["last_alert_status"] if prev else None,
                        prev["last_alert_at"] if prev else None, now)
            self._send(200, json.dumps({"status": status}), "application/json")
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    cfg = load_config(args.config)
    if cfg["token"] == "change-me":
        print("WARNING: using the default token; set one in your config file")
    store = Store(cfg["db_path"])
    threading.Thread(target=stale_loop, args=(cfg, store), daemon=True).start()
    srv = ThreadingHTTPServer((cfg["listen_host"], cfg["listen_port"]), make_handler(cfg, store))
    print(f"diskwatch listening on {cfg['listen_host']}:{cfg['listen_port']}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
