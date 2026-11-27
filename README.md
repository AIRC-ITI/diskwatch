# diskwatch

Disk health tracking for Linux servers. A small agent on each server reports SMART data to a central server, which shows a dashboard and sends Slack/email alerts. Python 3 standard library only.

## Central server

```bash
sudo mkdir -p /opt/diskwatch /etc/diskwatch
sudo cp server.py /opt/diskwatch/
sudo cp config.example.json /etc/diskwatch/config.json   # edit: token, Slack webhook, SMTP
sudo cp systemd/diskwatch-server.service /etc/systemd/system/
sudo systemctl enable --now diskwatch-server
```

Dashboard: `http://<server>:8080/`  ·  JSON: `/api/status`  ·  health check: `/healthz`

Put it behind a reverse proxy with HTTPS if agents report over an untrusted network. The dashboard itself has no login.

## Each monitored server

```bash
sudo apt install smartmontools        # or: dnf install smartmontools (need >= 7.0)
sudo mkdir -p /opt/diskwatch /etc/diskwatch
sudo cp agent.py /opt/diskwatch/
printf 'DISKWATCH_SERVER=http://monitor.example.com:8080\nDISKWATCH_TOKEN=<same token>\n' | sudo tee /etc/diskwatch/agent.env
sudo chmod 600 /etc/diskwatch/agent.env
sudo cp systemd/diskwatch-agent.* /etc/systemd/system/
sudo systemctl enable --now diskwatch-agent.timer
```

Try it first: `sudo python3 /opt/diskwatch/agent.py --dry-run`

## Status rules (tune in `thresholds`)

| Status | Triggers |
|---|---|
| CRIT | SMART overall check failed; NVMe critical warning or spare below threshold; wear >= 90%; reallocated sectors >= 50; temp >= 70C |
| WARN | wear >= 80%; any reallocated, pending or uncorrectable sectors; NVMe media errors; temp >= 60C |
| STALE | host has not reported for `stale_minutes` (default 90) |

Alerts fire when a host's status changes, again every `realert_hours` while unresolved, and when it recovers.

## Notes

- Disks behind hardware RAID controllers may need `smartctl -d megaraid,N` style options; `smartctl --scan` handles some but not all cases.
- VMs usually expose no SMART data and will show a WARN "no disks reported".
- History is kept in SQLite (`readings` table, `history_days`) for future trend views.
