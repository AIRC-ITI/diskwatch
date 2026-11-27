#!/usr/bin/env bash
# install.sh - install diskwatch on this machine as systemd service(s).
#
# Usage:
#   sudo ./install.sh server [--port 8080] [--token TOKEN]
#   sudo ./install.sh agent  --server http://monitor.example.com:8080 --token TOKEN
#
# server  Installs the central server (dashboard + alerts). Generates a token on the
#         first install and prints it; an existing config is never overwritten.
# agent   Installs smartmontools if missing, the agent, and a timer that reports
#         every 30 minutes. Prompts for --server/--token if run interactively.
#
# Safe to re-run: it updates the program files and keeps your config.
# Both modes can be installed on the same machine.
set -euo pipefail

ROOT="${DISKWATCH_ROOT:-}"          # for testing only; empty on a real system
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$ROOT/opt/diskwatch"
CONF_DIR="$ROOT/etc/diskwatch"
UNIT_DIR="$ROOT/etc/systemd/system"
SVC_USER="diskwatch"

info() { echo "==> $*"; }
warn() { echo "warning: $*" >&2; }
die()  { echo "error: $*" >&2; exit 1; }
usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; }

MODE="${1:-}"
[[ -z "$MODE" || "$MODE" == "-h" || "$MODE" == "--help" ]] && { usage; exit 0; }
shift

PORT=8080
TOKEN=""
SERVER_URL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)   PORT="${2:-}"; shift 2 ;;
    --token)  TOKEN="${2:-}"; shift 2 ;;
    --server) SERVER_URL="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

[[ "$MODE" == "server" || "$MODE" == "agent" ]] || die "first argument must be 'server' or 'agent'"
[[ -z "$ROOT" && $EUID -ne 0 ]] && die "please run as root: sudo $0 $MODE ..."
command -v python3 >/dev/null 2>&1 || die "python3 is required"
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) || die "invalid --port"
[[ -z "$TOKEN" || "$TOKEN" =~ ^[^[:space:]]+$ ]] || die "the token must not contain spaces"

HAVE_SYSTEMD=0
if [[ -z "$ROOT" ]] && command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
  HAVE_SYSTEMD=1
fi
[[ -z "$ROOT" && $HAVE_SYSTEMD -eq 0 ]] && die "systemd was not found; see the README for a manual setup"

need_file() { [[ -f "$SRC/$1" ]] || die "missing $1 next to install.sh (run it from the diskwatch folder)"; }

ensure_smartctl() {
  if ! command -v smartctl >/dev/null 2>&1; then
    if [[ -n "$ROOT" ]]; then info "(test mode) skipping smartmontools install"; return; fi
    info "Installing smartmontools"
    if   command -v apt-get >/dev/null 2>&1; then apt-get update -qq && apt-get install -y smartmontools
    elif command -v dnf     >/dev/null 2>&1; then dnf install -y smartmontools
    elif command -v yum     >/dev/null 2>&1; then yum install -y smartmontools
    elif command -v zypper  >/dev/null 2>&1; then zypper --non-interactive install smartmontools
    elif command -v pacman  >/dev/null 2>&1; then pacman -S --noconfirm smartmontools
    else die "could not install smartmontools automatically; install it and re-run"
    fi
  fi
  local ver major
  ver="$(smartctl --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+' | head -1 || true)"
  major="${ver%%.*}"
  if [[ -n "$major" && "$major" -lt 7 ]]; then
    warn "smartctl $ver is too old; diskwatch needs 7.0 or newer for JSON output"
  fi
}

reload_systemd() { [[ $HAVE_SYSTEMD -eq 1 ]] && systemctl daemon-reload || true; }

# ------------------------------------------------------------------ server
install_server() {
  need_file server.py
  need_file systemd/diskwatch-server.service
  local conf="$CONF_DIR/config.json" new_token=""

  info "Installing the diskwatch server"
  if [[ -z "$ROOT" ]] && ! id "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
  fi
  mkdir -p "$APP_DIR" "$CONF_DIR"
  install -m 0644 "$SRC/server.py" "$APP_DIR/server.py"

  # Older installs ran with a transient (DynamicUser) account; move their data to the real one.
  local legacy="$ROOT/var/lib/private/diskwatch" state="$ROOT/var/lib/diskwatch"
  if [[ -d "$legacy" && -L "$state" ]]; then
    info "Migrating existing data to /var/lib/diskwatch"
    [[ $HAVE_SYSTEMD -eq 1 ]] && systemctl stop diskwatch-server >/dev/null 2>&1 || true
    rm -f "$state"
    mv "$legacy" "$state"
    [[ -z "$ROOT" ]] && chown -R "$SVC_USER:$SVC_USER" "$state"
  fi

  if [[ -f "$conf" ]]; then
    info "Keeping the existing config ($conf)"
  else
    new_token="${TOKEN:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')}"
    python3 - "$conf" "$PORT" "$new_token" <<'PY'
import json, sys
path, port, token = sys.argv[1], int(sys.argv[2]), sys.argv[3]
cfg = {"listen_host": "0.0.0.0", "listen_port": port, "token": token,
       "db_path": "/var/lib/diskwatch/diskwatch.db"}
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PY
    chmod 640 "$conf"
    [[ -z "$ROOT" ]] && chown "root:$SVC_USER" "$conf"
  fi

  install -m 0644 "$SRC/systemd/diskwatch-server.service" "$UNIT_DIR/diskwatch-server.service"
  reload_systemd
  if [[ $HAVE_SYSTEMD -eq 1 ]]; then
    systemctl enable diskwatch-server >/dev/null 2>&1
    systemctl restart diskwatch-server
  fi

  local port
  port="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("listen_port", 8080))' "$conf")"
  if [[ $HAVE_SYSTEMD -eq 1 ]]; then
    local ok=0
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      if python3 -c 'import sys,urllib.request; urllib.request.urlopen("http://127.0.0.1:%s/healthz" % sys.argv[1], timeout=2)' "$port" 2>/dev/null; then ok=1; break; fi
      sleep 1
    done
    [[ $ok -eq 1 ]] && info "Server is running" || warn "server did not answer; check: journalctl -u diskwatch-server -n 50"
  fi

  echo
  echo "Dashboard:  http://<this-machine>:$port/"
  if [[ -n "$new_token" ]]; then
    echo "Token:      $new_token"
    echo
    echo "Save the token: every agent needs it. Install an agent with:"
    echo "  sudo ./install.sh agent --server http://<this-machine>:$port --token <token>"
  else
    echo "Token:      unchanged (see $conf)"
  fi
  echo "Alerts (Slack, Telegram, email) are optional: add them to $conf and run:"
  echo "  sudo systemctl restart diskwatch-server"
  echo "Open port $port to your agents if a firewall is on (e.g. sudo ufw allow $port/tcp)."
}

# ------------------------------------------------------------------ agent
install_agent() {
  need_file agent.py
  need_file systemd/diskwatch-agent.service
  need_file systemd/diskwatch-agent.timer

  if [[ -z "$SERVER_URL" && -t 0 ]]; then read -r -p "Diskwatch server URL (e.g. http://monitor:8080): " SERVER_URL; fi
  if [[ -z "$TOKEN" && -t 0 ]]; then read -r -s -p "Token: " TOKEN; echo; fi
  [[ -n "$SERVER_URL" && -n "$TOKEN" ]] || die "--server and --token are required"
  [[ "$SERVER_URL" =~ ^https?://[^[:space:]]+$ ]] || die "--server must look like http://host:port"
  [[ "$TOKEN" =~ ^[^[:space:]]+$ ]] || die "the token must not contain spaces"

  info "Installing the diskwatch agent"
  ensure_smartctl
  mkdir -p "$APP_DIR" "$CONF_DIR"
  install -m 0644 "$SRC/agent.py" "$APP_DIR/agent.py"

  ( umask 077; printf 'DISKWATCH_SERVER=%s\nDISKWATCH_TOKEN=%s\n' "$SERVER_URL" "$TOKEN" > "$CONF_DIR/agent.env" )
  chmod 600 "$CONF_DIR/agent.env"

  install -m 0644 "$SRC/systemd/diskwatch-agent.service" "$UNIT_DIR/diskwatch-agent.service"
  install -m 0644 "$SRC/systemd/diskwatch-agent.timer" "$UNIT_DIR/diskwatch-agent.timer"
  reload_systemd
  [[ $HAVE_SYSTEMD -eq 1 ]] && systemctl enable --now diskwatch-agent.timer >/dev/null 2>&1

  info "Sending a first report"
  if DISKWATCH_SERVER="$SERVER_URL" DISKWATCH_TOKEN="$TOKEN" python3 "$APP_DIR/agent.py"; then
    echo "The host should now appear on the dashboard."
  else
    warn "the first report failed; check the server URL, token and firewall, then run:"
    warn "  sudo systemctl start diskwatch-agent.service && journalctl -u diskwatch-agent -n 20"
  fi
  echo "The agent reports every 30 minutes (timer: diskwatch-agent.timer)."
}

case "$MODE" in
  server) install_server ;;
  agent)  install_agent ;;
esac