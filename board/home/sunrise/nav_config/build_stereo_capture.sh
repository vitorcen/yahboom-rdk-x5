#!/bin/bash
# Rebuild stereo_capture on the board from a fresh RDK OS image.
# 重刷机后重建双目采集链:左眼配置 + 注册 patch + 编译,全部幂等可重放。
#
# What it does (idempotent):
#  1. Generate the left-eye sc132gs config (addr 0x32) as a deterministic sed of
#     the stock 1088x1280 config, into /app/multimedia_samples/vp_sensors/sc132gs/.
#  2. Register it in vp_sensors.c (extern + config list) if not already there.
#  3. make the vp_sensors .o pool (via get_vin_data's Makefile).
#  4. gcc stereo_capture from ~/nav_config/stereo_capture.c.
# After this, stereo_cam.py's CAPTURE cmd (-s 4 -s 5) works; index 5 = left eye.
set -e
VPS=/app/multimedia_samples/vp_sensors
SRC=$VPS/sc132gs/linear_1088x1280_raw10_30fps_1lane.c
DST=$VPS/sc132gs/linear_1088x1280_raw10_30fps_1lane_left.c
CFG=/home/sunrise/nav_config

sed -e "s/sc132gs_linear_1088x1280_raw10_30fps_1lane\b/&_left/" \
    -e "s/{0x30, 0x33}/{0x32}/" \
    -e "s/\"sc132gs-1280p\"/\"sc132gs-left\"/" "$SRC" > "$DST"

# Load the sensor's ISP tuning (CCM/gamma/LSC). The stock config ships
# calib_lname="disable" which leaves ISP color correction at defaults ->
# magenta/green split cast. Patch BOTH eyes (idempotent).
for f in "$SRC" "$DST"; do
    sed -i 's|\.calib_lname = "disable"|.calib_lname = "/usr/hobot/bin/sc132gs_tuning.json"|' "$f"
done

if ! grep -q "_1lane_left" $VPS/vp_sensors.c; then
    sed -i "s/^extern vp_sensor_config_t sc132gs_linear_1088x1280_raw10_30fps_1lane;/&\nextern vp_sensor_config_t sc132gs_linear_1088x1280_raw10_30fps_1lane_left;/" $VPS/vp_sensors.c
    sed -i "s/\t&sc132gs_linear_1088x1280_raw10_30fps_1lane,/&\n\t\&sc132gs_linear_1088x1280_raw10_30fps_1lane_left,/" $VPS/vp_sensors.c
fi

make -C /app/multimedia_samples/sample_vin/get_vin_data >/dev/null

gcc -O2 -o "$CFG/stereo_capture" "$CFG/stereo_capture.c" \
    -I/usr/hobot/include -I/app/multimedia_samples/include \
    -I/app/multimedia_samples/utils -I$VPS \
    /app/multimedia_samples/utils/common_utils.o \
    $(find $VPS -name '*.o') \
    -L/usr/hobot/lib -lcam -lvpf -lhbmem -lgdcbin -lcjson -lpthread -lalog -ldl

echo "OK: $CFG/stereo_capture ($($CFG/stereo_capture -h 2>/dev/null | grep -c sc132gs) sc132gs configs registered)"
