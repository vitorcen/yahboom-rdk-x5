#!/usr/bin/env bash
# One-shot restore of all board-side files after a fresh reflash.
# board/ mirrors the board filesystem 1:1, so restore == rsync + enable units.
#
# Usage: ./deploy_board.sh [board-ip]      (default 192.168.13.187)
# Prereq: ssh root@<ip> works (password `yahboom`, or run ssh-copy-id first).
set -euo pipefail

IP=${1:-192.168.13.187}
REPO=$(cd "$(dirname "$0")/.." && pwd)

echo "== 1/3 sync board/ -> root@$IP:/ =="
rsync -av "$REPO/board/" "root@$IP:/"

echo "== 2/3 enable autostart units =="
ssh "root@$IP" 'systemctl daemon-reload && systemctl enable --now ms200-lidar rosbridge'

echo "== 3/3 verify =="
ssh "root@$IP" 'systemctl is-active ms200-lidar rosbridge && ss -tln | grep -q 9090 && echo "OK: /scan driver + ws://:9090 up"'
echo "Done. Open docs/lidar-live-viewer.html in a browser for the live view."
