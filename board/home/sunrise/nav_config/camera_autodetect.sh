#!/bin/bash
# Pick the camera preview source by what is actually plugged in, then exec it.
# 开机检测插了什么就用什么:CSI GS130WI 双目 → stereo_cam.py;USB Astra → astra_preview.py。
#
# Contract (both sources publish the same topics the GUI subscribes):
#   /image_jpeg               right window  #cambox   (color)
#   /camera/depth/color_jpeg  left  window  #depthbox (depth; astra now, stereo later)
#
# Detection is by hardware identity, not guesswork:
#   - GS130WI stereo = the module's factory-calibration EEPROM at i2c-6 0x50, whose
#     first 5 bytes are ASCII "UNION". This is MCLK-independent (unlike the SC132GS
#     chip-id, which NAKs once a pipeline has powered the sensor down), so it detects
#     the module reliably at boot AND on any service restart.
#   - Astra Pro depth = USB 2bc5:0403 (the OpenNI depth endpoint).
set -u
CFG=/home/sunrise/nav_config

have_gs130wi() {
    # "UNION" = 0x55 0x4e 0x49 0x4f 0x4e ; the EEPROM rides the eye's FPC cable,
    # so it can be on either CSI i2c bus (6 or 4) depending on how cables are seated.
    for bus in 6 4; do
        [ "$(i2ctransfer -y $bus w2@0x50 0x00 0x00 r5 2>/dev/null)" = "0x55 0x4e 0x49 0x4f 0x4e" ] && return 0
    done
    return 1
}

have_astra() { lsusb 2>/dev/null | grep -qi "2bc5:0403"; }

if have_gs130wi; then
    echo "camera_autodetect: GS130WI stereo (SC132GS) on CSI -> stereo_cam.py"
    exec python3 "$CFG/stereo_cam.py"
elif have_astra; then
    echo "camera_autodetect: USB Orbbec Astra Pro -> astra_preview.py"
    exec python3 "$CFG/astra_preview.py"
else
    echo "camera_autodetect: no known camera detected (no SC132GS on i2c-4/6, no Astra 2bc5:0403)"
    exec sleep infinity      # stay up; systemd keeps the unit active, replug + restart works
fi
