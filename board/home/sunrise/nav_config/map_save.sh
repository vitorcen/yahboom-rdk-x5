#!/bin/bash
# Save the live cartographer map over the nav map, then switch back to nav2.
# Keeps one backup generation (room.bak.*) so a bad save is recoverable.
set -eo pipefail
source /opt/tros/humble/setup.bash
export ROS_DOMAIN_ID=99 HOME=/root ROS_LOG_DIR=/tmp/roslog
MAP=/home/sunrise/maps/room

if ! systemctl is-active --quiet mapping; then
  echo "ERR: mapping.service not running — nothing to save" >&2
  exit 1
fi

[ -f "$MAP.yaml" ] && cp -f "$MAP.yaml" "$MAP.bak.yaml" && cp -f "$MAP.pgm" "$MAP.bak.pgm"

# map_saver_cli subscribes /map; the occupancy_grid node republishes every 1 s
ros2 run nav2_map_server map_saver_cli -f "$MAP" --ros-args -p save_map_timeout:=10.0

systemctl stop mapping
systemctl start nav2
echo ok
