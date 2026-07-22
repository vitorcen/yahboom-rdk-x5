#!/bin/bash
# Follow-me bringup: BPU perception chain + follow node, one foreground
# process group (Ctrl-C / SIGTERM stops everything; mux then streams zeros).
# Never opens a sensor itself — it rides whatever camera service is already
# streaming, picked by the same hardware probe camera_autodetect.sh uses.
set -e
cd "$(dirname "$0")"
source /opt/tros/humble/setup.bash
export ROS_DOMAIN_ID=99

# Camera source + geometry for follow_me.py. GS130WI stereo is identified by
# its factory-calibration EEPROM ("UNION" header @0x50, bus follows the CSI
# ribbon) — same probe as camera_autodetect.sh. Its stream for the BPU chain
# is /image_color_full (right eye 0x30, native 1088x1280, fx 666.5 cx 526.8
# from the factory calib; shoulder 0.45m*fx). Native res on purpose: on the
# 544 preview scale a hand is ~40px and the gesture classifier returns 0.
# Fallback = legacy IMX219 mipi /image_raw with follow_me.py's built-in
# defaults (960x544, 62 deg HFOV).
IMG_TOPIC=/image_raw
for bus in 6 4; do
  if i2ctransfer -y "$bus" w2@0x50 0x00 0x00 r5 2>/dev/null \
      | grep -q "0x55 0x4e 0x49 0x4f 0x4e"; then
    IMG_TOPIC=/image_color_full
    export FOLLOW_IMG_W=1088 FOLLOW_FX=666.5 FOLLOW_CX=526.8 FOLLOW_SHOULDER=300
    break
  fi
done
echo "follow camera: $IMG_TOPIC"

# model configs must sit in the working directory (tros convention)
for p in mono2d_body_detection hand_lmk_detection hand_gesture_detection; do
  [ -d config ] && [ -e "config/.${p}" ] || { cp -rn /opt/tros/humble/lib/$p/config .; touch "config/.${p}"; }
done
# MOT tuning (idempotent): min_score 0.8 drops low-confidence detections and
# the track id changes whenever the person turns around — keep them tracked;
# euclidean_thres 200 px is too tight for fast walkers at 30 fps.
sed -i 's/"min_score": 0\.[0-9]*/"min_score": 0.3/; s/"euclidean_thres": 200/"euclidean_thres": 300/' config/iou2*.json

# kill only the background children — `kill 0` would nuke the script itself,
# which under systemd reads as a signal death and triggers a restart loop
trap 'kill $(jobs -p) 2>/dev/null' EXIT INT TERM
ros2 run mono2d_body_detection mono2d_body_detection --ros-args \
  -p is_shared_mem_sub:=0 -p ros_img_topic_name:=$IMG_TOPIC --log-level warn &
# hand_lmk has NO image-topic param — /image_raw is hardcoded in the node
# (it only exposes ai_msg_*/is_shared_mem_sub), so the stereo stream must be
# wired in via topic remap. Harmless self-remap in the legacy /image_raw case.
ros2 run hand_lmk_detection hand_lmk_detection --ros-args \
  -r /image_raw:=$IMG_TOPIC -p is_shared_mem_sub:=0 \
  -p ai_msg_sub_topic_name:=/hobot_mono2d_body_detection --log-level warn &
ros2 run hand_gesture_detection hand_gesture_detection --ros-args \
  -p ai_msg_sub_topic_name:=/hobot_hand_lmk_detection --log-level warn &
sleep 3
python3 follow_me.py
