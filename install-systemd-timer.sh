#!/usr/bin/env bash
# Install systemd timer to run poly_dispute_report.py every hour.
# Usage: ./install-systemd-timer.sh [PROJECT_DIR]
#   PROJECT_DIR defaults to the directory containing this script.
# Requires: sudo (to install under /etc/systemd/system and enable timer).

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PROJECT_DIR="${1:-$SCRIPT_DIR}"
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"

if [[ ! -f "$PROJECT_DIR/poly_dispute_report.py" ]]; then
  echo "Error: poly_dispute_report.py not found in $PROJECT_DIR" >&2
  exit 1
fi
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  echo "Error: .env not found in $PROJECT_DIR" >&2
  exit 1
fi

RUN_USER="${SUDO_USER:-$USER}"
RUN_GROUP="${SUDO_GID:+$(getent group "$SUDO_GID" | cut -d: -f1)}"
RUN_GROUP="${RUN_GROUP:-$(id -gn "$RUN_USER")}"

UNITS_DIR="$PROJECT_DIR/systemd"
SERVICE_SRC="$UNITS_DIR/holy-poly-dispute-research.service"
TIMER_SRC="$UNITS_DIR/holy-poly-dispute-research.timer"
DEST_DIR="/etc/systemd/system"

for f in "$SERVICE_SRC" "$TIMER_SRC"; do
  if [[ ! -f "$f" ]]; then
    echo "Error: $f not found" >&2
    exit 1
  fi
done

echo "Project dir: $PROJECT_DIR"
echo "Run as user: $RUN_USER ($RUN_GROUP)"
echo "Installing to $DEST_DIR and enabling timer (requires sudo)."

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

sed -e "s|REPLACE_PROJECT_DIR|$PROJECT_DIR|g" \
    -e "s|REPLACE_USER|$RUN_USER|g" \
    -e "s|REPLACE_GROUP|$RUN_GROUP|g" \
    "$SERVICE_SRC" > "$TMP/holy-poly-dispute-research.service"
cp "$TIMER_SRC" "$TMP/holy-poly-dispute-research.timer"

sudo cp "$TMP/holy-poly-dispute-research.service" "$DEST_DIR/"
sudo cp "$TMP/holy-poly-dispute-research.timer" "$DEST_DIR/"
sudo systemctl daemon-reload
sudo systemctl enable holy-poly-dispute-research.timer
sudo systemctl start holy-poly-dispute-research.timer

echo "Done. Timer is enabled and started."
echo "  Status:  sudo systemctl status holy-poly-dispute-research.timer"
echo "  Logs:    sudo journalctl -u holy-poly-dispute-research.service -f"
echo "  Next:    systemctl list-timers holy-poly-dispute-research.timer"
