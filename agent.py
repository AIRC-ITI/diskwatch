#!/usr/bin/env python3
"""diskwatch agent: collect SMART data with smartctl and report it to the central server.

Run as root (smartctl needs it). Requires smartmontools (smartctl >= 7.0 for JSON output).

Usage:
  agent.py --server http://monitor.example.com:8080 --token SECRET
  agent.py --dry-run        # print the report instead of sending it
Environment alternatives: DISKWATCH_SERVER, DISKWATCH_TOKEN
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request


VIRTUAL_WORDS = ("qemu", "vmware", "virtual", "vbox", "msft", "hyper-v", "xen", "bhyve")


def run_json(cmd):
    """Run a command and parse its JSON stdout. smartctl uses non-zero exit codes
    as bit flags even when output is valid, so we ignore the return code."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        sys.exit("smartctl not found. Install smartmontools.")
    except subprocess.TimeoutExpired:
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return None


def ata_attr(data, attr_id):
    for a in data.get("ata_smart_attributes", {}).get("table", []):
        if a.get("id") == attr_id:
            return a.get("raw", {}).get("value")
    return None


def collect_device(dev, dtype):
    # `smartctl --scan` labels most SATA disks "-d scsi". Forcing that type hides the ATA SMART
    # data (temperature, wear, sectors), so for scsi let smartctl auto-detect, then try SAT.
    attempts = [None, "sat"] if dtype in (None, "scsi") else [dtype]
    best = None
    for t in attempts:
        cmd = ["smartctl", "-a", "-j"] + (["-d", t] if t else []) + [dev]
        d = run_json(cmd)
        if not d:
            continue
        out = parse_device(d, dev, t or dtype)
        if best is None:
            best = out
        if not out["no_smart"]:
            return out
    return best or {"device": dev, "error": "smartctl returned no data"}


def parse_device(d, dev, dtype):

    # SCSI/SAS/SAT/virtual devices report vendor+product instead of model_name.
    vendor, product = d.get("vendor"), d.get("product")
    model = d.get("model_name") or " ".join(
        x for x in (vendor if vendor not in (None, "ATA") else None, product) if x) or None

    out = {
        "device": dev,
        "type": d.get("device", {}).get("type", dtype),
        "model": model,
        "serial": d.get("serial_number"),
        "firmware": d.get("firmware_version") or d.get("revision"),
        "capacity_bytes": d.get("user_capacity", {}).get("bytes"),
        "rotation_rate": d.get("rotation_rate"),
        "smart_passed": d.get("smart_status", {}).get("passed"),
        # virtual disks report 0 here; treat 0 as "unknown"
        "temperature_c": d.get("temperature", {}).get("current") or None,
        "power_on_hours": d.get("power_on_time", {}).get("hours"),
    }

    nvme = d.get("nvme_smart_health_information_log")
    if nvme:
        out.update({
            "nvme_critical_warning": nvme.get("critical_warning"),
            "nvme_percentage_used": nvme.get("percentage_used"),
            "nvme_available_spare": nvme.get("available_spare"),
            "nvme_spare_threshold": nvme.get("available_spare_threshold"),
            "nvme_media_errors": nvme.get("media_errors"),
            "nvme_unsafe_shutdowns": nvme.get("unsafe_shutdowns"),
        })
    else:
        out.update({
            "reallocated_sectors": (ata_attr(d, 5) if ata_attr(d, 5) is not None
                                    else d.get("scsi_grown_defect_list")),
            "pending_sectors": ata_attr(d, 197),
            "offline_uncorrectable": ata_attr(d, 198),
            "crc_errors": ata_attr(d, 199),
            "ssd_wear_used_pct": None,
        })
        # Wear indicators differ by vendor: 177 (Samsung wear leveling), 231 (SSD life left), 233 (Media wearout)
        for aid in (177, 231, 233):
            for a in d.get("ata_smart_attributes", {}).get("table", []):
                if a.get("id") == aid and a.get("value") is not None:
                    out["ssd_wear_used_pct"] = 100 - a["value"]
        if out["temperature_c"] is None:
            out["temperature_c"] = ata_attr(d, 194)
            if out["temperature_c"] is not None:
                out["temperature_c"] &= 0xFF  # raw value may pack min/max in upper bytes
                out["temperature_c"] = out["temperature_c"] or None

    # SCSI error counters (SAS drives, some SATA behind HBAs)
    log = d.get("scsi_error_counter_log") or {}
    unc = [v["total_uncorrected_errors"] for v in log.values()
           if isinstance(v, dict) and v.get("total_uncorrected_errors") is not None]
    if unc:
        out["uncorrected_errors"] = sum(unc)

    # Flag disks that expose no usable health data (typically virtual disks in a VM).
    ident = " ".join(str(x) for x in (vendor, product, d.get("model_name")) if x).lower()
    has_health = bool(nvme or d.get("ata_smart_attributes") or log
                      or d.get("scsi_grown_defect_list") is not None)
    out["virtual"] = any(w in ident for w in VIRTUAL_WORDS)
    out["no_smart"] = out["virtual"] or not has_health
    return out


def collect():
    scan = run_json(["smartctl", "--scan", "-j"]) or {}
    disks = []
    for dev in scan.get("devices", []):
        disks.append(collect_device(dev["name"], dev.get("type")))
    return {
        "hostname": socket.getfqdn() or socket.gethostname(),
        "collected_at": int(time.time()),
        "disks": disks,
    }


def send(server, token, report):
    req = urllib.request.Request(
        server.rstrip("/") + "/api/report",
        data=json.dumps(report).encode(),
        headers={"Content-Type": "application/json", "X-Diskwatch-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=os.environ.get("DISKWATCH_SERVER"))
    ap.add_argument("--token", default=os.environ.get("DISKWATCH_TOKEN"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("warning: not running as root, smartctl may fail", file=sys.stderr)

    report = collect()
    if args.dry_run:
        print(json.dumps(report, indent=2))
        return
    if not args.server or not args.token:
        sys.exit("--server and --token are required (or set DISKWATCH_SERVER / DISKWATCH_TOKEN)")
    try:
        status = send(args.server, args.token, report)
    except Exception as e:
        sys.exit(f"failed to send report: {e}")
    print(f"reported {len(report['disks'])} disk(s), HTTP {status}")


if __name__ == "__main__":
    main()