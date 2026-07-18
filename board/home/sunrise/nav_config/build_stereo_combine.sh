#!/bin/bash
# Build the stereo_combine C++ node on the board (idempotent).
# 板上编译 C++ 热路径节点;产物拷到 ~/nav_config/stereo_combine 供 stereo_cam.py 拉起。
set -e
CFG=/home/sunrise/nav_config
WS=$CFG/.combine_ws

source /opt/tros/humble/setup.bash
mkdir -p "$WS/src"
ln -sfn "$CFG/stereo_combine_pkg" "$WS/src/stereo_combine"
cd "$WS"
colcon build --packages-select stereo_combine 2>&1 | tail -3
cp "$WS/install/stereo_combine/lib/stereo_combine/stereo_combine" "$CFG/stereo_combine_node"
echo "OK: $CFG/stereo_combine_node"
