#!/usr/bin/env bash
# Replay a pulled episode bag into a local RViz "video player".
#   ./replay.sh <path-to-episode-dir>   (expects <dir>/bag/ inside)
# Runs on ROS_DOMAIN_ID=42 so the replay can never cross-talk with the live
# robot on domain 99. bag play loops until the RViz window is closed.
# no `set -u`: ROS setup.bash reads unbound variables and would abort
set -eo pipefail
ep="${1:?usage: replay.sh <episode-dir>}"
[[ -d "$ep/bag" ]] || { echo "no bag/ inside $ep — pull the full episode first" >&2; exit 1; }
source /opt/ros/jazzy/setup.bash
export ROS_DOMAIN_ID=42
# --clock + use_sim_time: RViz follows the recorded timeline, otherwise TF
# lookups compare bag-time stamps against wall clock and drop everything.
ros2 bag play "$ep/bag" --loop --clock &
play=$!
trap 'kill $play 2>/dev/null || true' EXIT
rviz2 -d "$(dirname "$0")/robot.rviz" --ros-args -p use_sim_time:=true
