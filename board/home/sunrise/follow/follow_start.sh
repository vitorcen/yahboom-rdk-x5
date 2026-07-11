#!/bin/bash
# Follow-me bringup: BPU perception chain + follow node, one foreground
# process group (Ctrl-C / SIGTERM stops everything; mux then streams zeros).
# Reuses the always-on mipi-cam.service stream (/image_raw, 960x544 ros mode)
# instead of opening the sensor a second time.
set -e
cd "$(dirname "$0")"
source /opt/tros/humble/setup.bash
export ROS_DOMAIN_ID=99

# model configs must sit in the working directory (tros convention)
for p in mono2d_body_detection hand_lmk_detection hand_gesture_detection; do
  [ -d config ] && [ -e "config/.${p}" ] || { cp -rn /opt/tros/humble/lib/$p/config .; touch "config/.${p}"; }
done

trap 'kill 0' EXIT INT TERM
ros2 run mono2d_body_detection mono2d_body_detection --ros-args \
  -p is_shared_mem_sub:=0 -p ros_img_topic_name:=/image_raw --log-level warn &
ros2 run hand_lmk_detection hand_lmk_detection --ros-args \
  -p is_shared_mem_sub:=0 -p ros_img_topic_name:=/image_raw \
  -p ai_msg_sub_topic_name:=/hobot_mono2d_body_detection --log-level warn &
ros2 run hand_gesture_detection hand_gesture_detection --ros-args \
  -p ai_msg_sub_topic_name:=/hobot_hand_lmk_detection --log-level warn &
sleep 3
python3 follow_me.py
