# diskwatch

Disk health tracking for Linux servers. A small agent on each server reports SMART data to a central server, which shows a dashboard and sends Slack, Telegram and email alerts. Python 3 standard library only.

## Install (recommended)

Run from this folder, as root. Safe to re-run to update; your config is kept.

```bash
# 1. on the machine that hosts the dashboard (prints a token on first install)
sudo ./install.sh server [--port 8080]

# 2. on every machine whose disks you want to monitor (including the server itself if you like)
sudo ./install.sh agent --server http://<server>:8080 --token <token>
```

The server install creates a `diskwatch` system user, a config in `/etc/diskwatch/config.json` with a random token, and the `diskwatch-server` service. The agent install adds smartmontools if missing, `/etc/diskwatch/agent.env` (mode 600) and a timer that reports every 30 minutes, then sends a first report.

The manual steps below do the same thing by hand.

## Manual install: central server

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin diskwatch
sudo mkdir -p /opt/diskwatch /etc/diskwatch
sudo cp server.py /opt/diskwatch/
sudo cp config.example.json /etc/diskwatch/config.json   # edit: token, plus Slack / Telegram / SMTP for alerts
sudo chown root:diskwatch /etc/diskwatch/config.json && sudo chmod 640 /etc/diskwatch/config.json
sudo cp systemd/diskwatch-server.service /etc/systemd/system/
sudo systemctl enable --now diskwatch-server
```

Dashboard: `http://<server>:8080/`  ·  JSON: `/api/status`  ·  health check: `/healthz`

Put it behind a reverse proxy with HTTPS if agents report over an untrusted network. The dashboard itself has no login.

## Manual install: each monitored server

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

## Telegram alerts

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the bot token.
2. Add the bot to your chat or group (or just message it once for a private chat), then send any message there.
3. Find the chat id: open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `message.chat.id` (groups are negative numbers).
4. Put both in the config under `telegram` (`bot_token`, `chat_id`) and restart the server.

## Status rules (tune in `thresholds`)

| Status | Triggers |
|---|---|
| CRIT | SMART overall check failed; NVMe critical warning or spare below threshold; wear >= 90%; reallocated sectors >= 50; temp >= 70C |
| WARN | wear >= 80%; any reallocated, pending or uncorrectable sectors; uncorrected read/write errors (SAS/SCSI); NVMe media errors; temp >= 60C |
| STALE | host has not reported for `stale_minutes` (default 90) |
| N/A | no SMART data readable (virtual disks, some RAID/USB bridges). Shown in grey, never alerts |

SSD wear is read from the vendor's wear attribute (found by name, e.g. `Wear_Leveling_Count`, or by ID 173/177/231/233) and shown as % used.

Alerts fire when a host's status changes, again every `realert_hours` while unresolved, and when it recovers.

## Updating

After pulling new code, copy the files over again. Config and data are untouched.

```bash
# central server
sudo cp server.py /opt/diskwatch/ && sudo systemctl restart diskwatch-server

# each monitored machine (the timer picks up the new file; no restart needed)
sudo cp agent.py /opt/diskwatch/
sudo python3 /opt/diskwatch/agent.py --dry-run    # optional check
sudo systemctl start diskwatch-agent.service      # report now
```

## Uninstall

```bash
sudo ./uninstall.sh                 # shows what will be removed and asks you to type 'yes'
sudo ./uninstall.sh --keep-data     # keep /etc/diskwatch (config, token) and the server database
sudo ./uninstall.sh --yes           # no prompt (for scripts)
sudo ./uninstall.sh --remove-smartmontools   # also uninstall smartmontools
```

It stops and removes the systemd units, removes the host from the dashboard (skip with `--no-unregister`), and deletes `/opt/diskwatch`, `/etc/diskwatch`, `/var/lib/diskwatch` and the `diskwatch` user. Run it on every machine that has the agent, and on the server host.

A host stays on the dashboard until the server is told to forget it. If it is already uninstalled or the server was unreachable, remove it on the server:

```bash
python3 /opt/diskwatch/server.py --config /etc/diskwatch/config.json --remove-host <hostname>
```

(Or from an agent machine that is still installed: `sudo python3 /opt/diskwatch/agent.py --unregister` with `DISKWATCH_SERVER` and `DISKWATCH_TOKEN` set.)

## Notes

- Disks behind hardware RAID controllers may need `smartctl -d megaraid,N` style options; `smartctl --scan` handles some but not all cases.
- VMs usually expose only virtual disks with no SMART data. These show as grey N/A (never alerts); install the agent on the hypervisor (e.g. the Proxmox host) to monitor the physical disks.
- History is kept in SQLite (`readings` table, `history_days`) for future trend views.