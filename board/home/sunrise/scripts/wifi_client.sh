#!/bin/bash
# =============================================================================
# wifi_client.sh  —  切换 RDK X5 的 wlan0 从 AP 模式到 WiFi 客户端(station)模式
#                    并连接指定家用路由器。
# Switch wlan0 from AP mode to WiFi client mode and join a home router.
#
# 亚博这台车默认是 AP 热点模式：wlan0 被 hostapd 独占、NetworkManager 不管它
# (wlan0:unmanaged)，所以直接跑官方 `wifi_connect` 会扫不到任何 AP。本脚本先
# 释放 wlan0 交还 NM、设好国家码(放开 5G 扫描)、轮询扫描确认能看到目标 SSID，
# 再连接；失败自动回滚 AP，避免把自己锁在门外。
#
# 官方 STA 配网(地瓜文档)本质就三步，本脚本是在其之上补了"先退出 AP"：
#     sudo nmcli device wifi rescan
#     sudo nmcli device wifi list
#     sudo wifi_connect "SSID" "PASSWORD"     # = nmcli device wifi connect
#
# 用法 / Usage:  sudo ./wifi_client.sh                         # 交互输入密码
#                sudo ./wifi_client.sh "SSID" "PASSWORD"     # 临时指定
#                sudo ./wifi_client.sh "SSID" "PASSWORD" US  # 再指定国家码
#                sudo WIFI_PASSWORD="..." ./wifi_client.sh "SSID"
#
# ⚠️  成功后板子离开自己的 RDK_X5_Robot 热点、加入目标路由器，经 192.168.8.88
#     的 SSH 会断开。请到路由器后台/板子 OLED 屏看新 IP 再重连。
# =============================================================================
set -u

SSID="${1:-YOUR_WIFI_SSID_5G}"
PASS="${2:-${WIFI_PASSWORD:-}}"
REGDOM="${3:-CN}"                 # 国家码：CN=中国, US=美国。影响 5G 信道是否可扫描
SCAN_TRIES=10                     # 最多轮询扫描次数 (每次 3s，共 ~30s)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OPEN_AP_DESKTOP="/home/sunrise/.config/autostart/Open_AP.desktop"
ROLLBACK_UNIT="wifi-client-rollback-$$"

if [ "$(id -u)" -ne 0 ]; then exec sudo "$0" "$@"; fi

if [ -z "$PASS" ]; then
    if [ -t 0 ]; then
        read -r -s -p "WiFi password for $SSID: " PASS
        echo
    else
        echo "缺少 WiFi 密码。请传第二个参数或设置 WIFI_PASSWORD。" >&2
        exit 2
    fi
fi

# 独立于本脚本注册兜底回滚。即使 nmcli 或驱动调用卡死，热点也会在 90 秒后恢复。
systemd-run --quiet --unit="$ROLLBACK_UNIT" --on-active=90s "$SCRIPT_DIR/wifi_ap.sh"

# 厂商桌面登录后会延迟运行 open_AP.sh，重新 mask supplicant 并抢回 wlan0。
# 必须在停止 AP 前禁用；若后续失败，wifi_ap.sh 会把该项恢复。
if [ -f "$OPEN_AP_DESKTOP" ]; then
    if grep -q '^Hidden=' "$OPEN_AP_DESKTOP"; then
        sed -i 's/^Hidden=.*/Hidden=true/' "$OPEN_AP_DESKTOP"
    else
        printf '\nHidden=true\n' >> "$OPEN_AP_DESKTOP"
    fi
fi

echo "==> [1/5] 停止 AP 服务 (hostapd + dhcpd) / stopping AP services..."
systemctl stop isc-dhcp-server isc-dhcp-server6 >/dev/null 2>&1
killall -9 hostapd >/dev/null 2>&1
ip addr flush dev wlan0
sleep 1

echo "==> [2/5] 放开 5G：设 aic8800 国家码 = $REGDOM ..."
# aic8800 的国家码是驱动模块参数(custregd=Y 时忽略内核 iw reg)，必须重载模块。
# 写持久配置，重启后仍生效；若当前不是目标国家码则立即重载驱动。
echo "options aic8800_fdrv country_code=$REGDOM custregd=N" > /etc/modprobe.d/aic8800.conf
cur_cc=$(cat /sys/module/aic8800_fdrv/parameters/country_code 2>/dev/null)
if [ "$cur_cc" != "$REGDOM" ]; then
    echo "    当前国家码=$cur_cc，重载 aic8800 驱动使 $REGDOM 生效 (WiFi 瞬断)..."
    modprobe -r aic8800_fdrv >/dev/null 2>&1
    sleep 1
    modprobe aic8800_fdrv >/dev/null 2>&1        # 自动读取上面的 modprobe.d 配置
    for i in $(seq 1 10); do ip link show wlan0 >/dev/null 2>&1 && break; sleep 1; done
    sleep 2
    echo "    重载后国家码 = $(cat /sys/module/aic8800_fdrv/parameters/country_code 2>/dev/null)"
fi
iw reg set "$REGDOM" >/dev/null 2>&1
sleep 1

echo "==> [3/5] 启动 wpa_supplicant，并把 wlan0 交给 NetworkManager..."
# 厂商 AP 镜像会 mask wpa_supplicant.service。NetworkManager 依赖它完成
# STA 扫描和认证；只设置 managed=yes 会让 wlan0 永远停在 unavailable。
timeout 10 nmcli device set wlan0 managed no >/dev/null 2>&1
ip link set wlan0 down >/dev/null 2>&1
iw dev wlan0 set type managed >/dev/null 2>&1
ip link set wlan0 up >/dev/null 2>&1
systemctl unmask wpa_supplicant.service >/dev/null 2>&1
if ! systemctl restart wpa_supplicant.service; then
    echo "    wpa_supplicant 启动失败，回滚 AP 模式。"
    "$SCRIPT_DIR/wifi_ap.sh"
    exit 1
fi
timeout 10 nmcli radio wifi on
timeout 10 nmcli device set wlan0 managed yes >/dev/null 2>&1

nm_ready=0
for i in $(seq 1 15); do
    state=$(timeout 5 nmcli -g GENERAL.STATE device show wlan0 2>/dev/null | cut -d' ' -f1)
    if [ -n "$state" ] && [ "$state" != "20" ] && [ "$state" != "10" ]; then
        nm_ready=1
        break
    fi
    sleep 1
done
if [ "$nm_ready" -ne 1 ]; then
    echo "    NetworkManager 未能接管 wlan0（state=${state:-unknown}），回滚 AP 模式。"
    journalctl -u NetworkManager -u wpa_supplicant -n 30 --no-pager 2>/dev/null
    "$SCRIPT_DIR/wifi_ap.sh"
    exit 1
fi

echo "==> [4/5] 扫描并确认能看到 \"$SSID\" / scanning for target SSID..."
found=0
if [ "$(iwgetid wlan0 -r 2>/dev/null)" = "$SSID" ]; then
    found=1
    echo "    ✓ NetworkManager 已自动连接 $SSID，跳过扫描"
else
    for i in $(seq 1 "$SCAN_TRIES"); do
        timeout 10 nmcli device wifi rescan ifname wlan0 >/dev/null 2>&1
        sleep 3
        if [ "$(iwgetid wlan0 -r 2>/dev/null)" = "$SSID" ] ||
           timeout 10 nmcli -t -f SSID device wifi list 2>/dev/null | grep -qxF "$SSID"; then
            found=1; echo "    ✓ 第 $i 次扫描发现 $SSID"; break
        fi
        echo "    ...第 $i/$SCAN_TRIES 次扫描暂未发现，继续..."
    done
fi

if [ "$found" -ne 1 ]; then
    echo "==> ❌ 扫了 ${SCAN_TRIES} 次仍未发现 \"$SSID\"。"
    echo "    当前扫到的 AP 如下（供核对；若目标是 5G，检查国家码/信道）:"
    nmcli -f SSID,CHAN,FREQ,SIGNAL,SECURITY device wifi list 2>/dev/null | head -20 | sed 's/^/      /'
    echo "    → 回滚 AP 模式以保住访问 / reverting to AP mode."
    "$SCRIPT_DIR/wifi_ap.sh"
    exit 1
fi

echo "==> [5/5] 连接 \"$SSID\" / connecting..."
if [ "$(iwgetid wlan0 -r 2>/dev/null)" = "$SSID" ] ||
   timeout 40 nmcli device wifi connect "$SSID" password "$PASS" ifname wlan0; then
    sleep 3
    IP=$(ip -4 -o addr show wlan0 | awk '{print $4}')
    systemctl stop "${ROLLBACK_UNIT}.timer" "${ROLLBACK_UNIT}.service" >/dev/null 2>&1
    systemctl reset-failed "${ROLLBACK_UNIT}.service" >/dev/null 2>&1
    echo "==> ✅ 已进入客户端模式 / client mode OK.  wlan0 = ${IP:-<no ip>}"
    echo "    AP (RDK_X5_Robot) 已关闭。请用上面的新 IP 重新 SSH。"
else
    echo "==> ❌ 连接失败（密码错误？信号弱？）— 回滚 AP 模式..."
    echo "    Connect FAILED — reverting to AP mode to keep access."
    "$SCRIPT_DIR/wifi_ap.sh"
    exit 1
fi
