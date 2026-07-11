#!/usr/bin/env bash
# Start (or restart) the full navigation stack on the board.
# Usage: sudo bash /home/sunrise/nav_config/nav_start.sh [map.yaml]
# Lidar / rosbridge / camera are systemd-enabled already; this adds:
#   nav-bringup : chassis driver + odom/EKF + joystick + cmd_vel mux
#   nav2        : AMCL + planner + controller on the saved map
# then seeds AMCL with a wide initial pose at the map origin.
set -euo pipefail

MAP=${1:-/home/sunrise/maps/room.yaml}
PARAMS=/home/sunrise/maps/nav_params_tuned.yaml
SRC="source /opt/tros/humble/setup.bash; source /home/sunrise/software/library_ws/install/setup.bash; source /home/sunrise/yahboomcar_ws/install/setup.bash; export ROS_DOMAIN_ID=99"

# Yahboom APP fights over the chassis serial — make sure it is gone.
pkill -f "app_Sunrise[R]obot.py" 2>/dev/null || true

# nav-bringup is a boot-enabled unit now; only (re)start it if not running.
systemctl is-active --quiet nav-bringup || systemctl restart nav-bringup
systemctl stop nav2 2>/dev/null || true
systemd-run --unit=nav2 --collect --setenv=HOME=/root --setenv=ROS_LOG_DIR=/tmp/roslog \
  bash -c "$SRC; exec ros2 launch yahboomcar_nav navigation_dwb_launch.py map:=$MAP params_file:=$PARAMS"

echo "waiting for Nav2 to subscribe /goal_pose ..."
for i in $(seq 1 40); do
  if bash -c "$SRC; ros2 topic info /goal_pose 2>/dev/null" | grep -q "Subscription count: [1-9]"; then
    break
  fi
  sleep 3
done

bash -c "$SRC; ros2 topic pub -t 3 -w 1 /initialpose geometry_msgs/msg/PoseWithCovarianceStamped \
  '{header: {frame_id: map}, pose: {pose: {position: {x: 0.0, y: 0.0}, orientation: {w: 1.0}},
    covariance: [1.0,0,0,0,0,0, 0,1.0,0,0,0,0, 0,0,0,0,0,0, 0,0,0,0,0,0, 0,0,0,0,0,0, 0,0,0,0,0,0.3]}}'" >/dev/null

echo "nav stack up: drag a goal in docs/lidar-live-viewer.html"
echo "stop with:   systemctl stop nav-bringup nav2"
