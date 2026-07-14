#!/usr/bin/env bash
# Launch RViz2 on the workstation, subscribed to the robot over Wi-Fi DDS.
# Workstation runs ROS Jazzy; the board runs Humble — standard messages
# (LaserScan/TF/Map/Odometry) are wire-compatible, verified 2026-07-13.
# no `set -u`: ROS setup.bash reads unbound variables and would abort
set -eo pipefail
source /opt/ros/jazzy/setup.bash
export ROS_DOMAIN_ID=99
exec rviz2 -d "$(dirname "$0")/robot.rviz" "$@"
