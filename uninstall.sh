#!/usr/bin/env bash
# uninstall.sh - remove diskwatch (agent and/or server) from this machine.
#
# Removes: systemd units, /opt/diskwatch, and (unless --keep-data) /etc/diskwatch
# (config, including the shared token) and /var/lib/diskwatch (server database).
# Leaves smartmontools installed unless --remove-smartmontools is given.
#
# Usage: sudo ./uninstall.sh [--yes] [--keep-data] [--remove-smartmontools]
set -euo pipefail

ROOT="${DISKWATCH_ROOT:-}"          # for testing only; empty on a real system
APP_DIR="$ROOT/opt/diskwatch"
CONF_DIR="$ROOT/etc/diskwatch"
DATA_DIRS=("$ROOT/var/lib/diskwatch" "$ROOT/var/lib/private/diskwatch")
UNIT_DIR="$ROOT/etc/systemd/system"
UNITS=(diskwatch-agent.timer diskwatch-agent.service diskwatch-server.service)

ASSUME_YES=0
KEEP_DATA=0
REMOVE_SMART=0

usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; }

for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    --keep-data) KEEP_DATA=1 ;;
    --remove-smartmontools) REMOVE_SMART=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$ROOT" && $EUID -ne 0 ]]; then
  echo "Please run as root: sudo $0" >&2
  exit 1
fi

HAVE_SYSTEMCTL=0
if [[ -z "$ROOT" ]] && command -v systemctl >/dev/null 2>&1; then
  HAVE_SYSTEMCTL=1
fi

# ---- find what is installed ----
found_units=()
for u in "${UNITS[@]}"; do
  [[ -e "$UNIT_DIR/$u" ]] && found_units+=("$u")
done

found_paths=()
[[ -e "$APP_DIR" ]] && found_paths+=("$APP_DIR")
data_paths=()
[[ -e "$CONF_DIR" || -L "$CONF_DIR" ]] && data_paths+=("$CONF_DIR")
for p in "${DATA_DIRS[@]}"; do
  [[ -e "$p" || -L "$p" ]] && data_paths+=("$p")
done

if [[ ${#found_units[@]} -eq 0 && ${#found_paths[@]} -eq 0 && ${#data_paths[@]} -eq 0 ]]; then
  echo "diskwatch is not installed here (nothing found). Nothing to do."
  exit 0
fi

# ---- show the plan ----
echo "This will remove diskwatch from this machine:"
echo
if [[ ${#found_units[@]} -gt 0 ]]; then
  echo "  Stop, disable and delete systemd units:"
  for u in "${found_units[@]}"; do echo "    - $u"; done
fi
for p in "${found_paths[@]}"; do echo "  Delete program files: $p"; done
if [[ ${#data_paths[@]} -gt 0 ]]; then
  if [[ $KEEP_DATA -eq 1 ]]; then
    echo "  KEEP config and data (--keep-data):"
    for p in "${data_paths[@]}"; do echo "    - $p"; done
  else
    echo "  Delete config and data (includes the token and stored disk history):"
    for p in "${data_paths[@]}"; do echo "    - $p"; done
  fi
fi
[[ $REMOVE_SMART -eq 1 ]] && echo "  Uninstall the smartmontools package"
echo

if pgrep -f "python[0-9.]* [^ ]*diskwatch/(server|agent)\.py" >/dev/null 2>&1; then
  echo "Note: a diskwatch process is still running; it will be stopped if it is a systemd service."
  echo "      Processes you started by hand must be stopped yourself."
  echo
fi

# ---- confirm ----
if [[ $ASSUME_YES -ne 1 ]]; then
  if [[ ! -t 0 ]]; then
    echo "Not running interactively; re-run with --yes to confirm." >&2
    exit 1
  fi
  read -r -p "Type 'yes' to continue: " answer
  if [[ "$answer" != "yes" ]]; then
    echo "Aborted. Nothing was changed."
    exit 1
  fi
fi

# ---- do it ----
for u in "${found_units[@]}"; do
  if [[ $HAVE_SYSTEMCTL -eq 1 ]]; then
    systemctl disable --now "$u" >/dev/null 2>&1 || true
  fi
  rm -f "$UNIT_DIR/$u"
  echo "removed unit $u"
done
if [[ $HAVE_SYSTEMCTL -eq 1 ]]; then
  systemctl daemon-reload
  systemctl reset-failed 'diskwatch-*' >/dev/null 2>&1 || true
fi

for p in "${found_paths[@]}"; do
  rm -rf "$p"
  echo "removed $p"
done

if [[ $KEEP_DATA -eq 0 ]]; then
  for p in "${data_paths[@]}"; do
    rm -rf "$p"
    echo "removed $p"
  done
else
  for p in "${data_paths[@]}"; do echo "kept $p"; done
fi

if [[ $REMOVE_SMART -eq 1 && -z "$ROOT" ]]; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get remove -y smartmontools
  elif command -v dnf >/dev/null 2>&1; then
    dnf remove -y smartmontools
  else
    echo "Could not detect apt or dnf; remove smartmontools yourself."
  fi
fi

echo
echo "diskwatch has been uninstalled."
echo "If the diskwatch server is still running elsewhere, it will mark this host as"
echo "STALE after a while; that is expected once the agent is gone."
