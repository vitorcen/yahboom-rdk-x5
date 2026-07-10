#!/bin/bash
# =============================================================================
# wifi_ap.sh  —  把 RDK X5 的 wlan0 恢复到出厂 AP 热点模式。
#                Restore wlan0 to the factory AP (hotspot) mode.
#
#   热点 SSID / SSID     : RDK_X5_Robot
#   热点密码 / password  : 12345678
#   AP 地址  / AP IP      : 192.168.8.88/24  (DHCP 派发 192.168.8.80~250)
#
# 用法 / Usage:  sudo ./wifi_ap.sh
#
# 恢复后：用电脑/手机连回 WiFi "RDK_X5_Robot"(密码 12345678)，
#        再  ssh root@192.168.8.88  (密码 yahboom)。
#
# 说明：复用厂商工具 `wifi_init ap`(它会重置 wlan0 并起 hostapd -B /etc/hostapd.conf)，
#      再补上静态 AP IP 与 DHCP 服务。恢复成功后，后续重启也会保持 AP 模式。
# =============================================================================
set -u

AP_IP="192.168.8.88/24"
OPEN_AP_DESKTOP="/home/sunrise/.config/autostart/Open_AP.desktop"

# 需要 root
if [ "$(id -u)" -ne 0 ]; then exec sudo "$0" "$@"; fi

echo "==> [1/4] 断开客户端 WiFi / disconnecting client WiFi..."
nmcli device disconnect wlan0 >/dev/null 2>&1
nmcli device set wlan0 managed no >/dev/null 2>&1
# 厂商 AP 镜像默认 mask 此服务；恢复该状态，避免 NetworkManager 与
# hostapd 同时争用 wlan0，也保证下次启动仍按厂商逻辑进入 AP 模式。
systemctl mask --now wpa_supplicant.service >/dev/null 2>&1
killall -9 wpa_supplicant >/dev/null 2>&1
sleep 1

echo "==> [2/4] 启动 hostapd 热点 (RDK_X5_Robot) / starting AP..."
# wifi_init ap: kill hostapd/wpa -> flush -> wlan0 down/up -> hostapd -B /etc/hostapd.conf
wifi_init ap
sleep 1

echo "==> [3/4] 配置 AP 静态地址 $AP_IP / assigning AP IP..."
ip addr add "$AP_IP" dev wlan0 >/dev/null 2>&1

echo "==> [4/4] 启动 DHCP 服务 / starting DHCP server for AP clients..."
systemctl restart isc-dhcp-server >/dev/null 2>&1
sleep 1

# 恢复厂商桌面登录时自动开启 AP 的行为。
if [ -f "$OPEN_AP_DESKTOP" ]; then
    if grep -q '^Hidden=' "$OPEN_AP_DESKTOP"; then
        sed -i 's/^Hidden=.*/Hidden=false/' "$OPEN_AP_DESKTOP"
    else
        printf '\nHidden=false\n' >> "$OPEN_AP_DESKTOP"
    fi
fi

echo "==> ✅ 已恢复 AP 模式 / AP mode restored.  wlan0:"
ip -4 -o addr show wlan0 | awk '{print "        "$4}'
echo "    连回 WiFi: SSID=RDK_X5_Robot  密码=12345678  然后 ssh root@192.168.8.88"
