#!/bin/bash
# =============================================================================
# wifi_diag.sh  —  WiFi 客户端扫描诊断（本地 HDMI/键鼠 下运行，会断 SSH）
#   停 AP → 用底层 iw 直接扫描(绕过 NetworkManager) → 打印所有可见 AP →
#   单独标出目标 SSID 与所有 5GHz AP → 结束后自动恢复 AP 模式。
#
# 目的：判断“扫不到 YOUR_WIFI_SSID_5G”到底是
#   (A) 目标是 5G、被 aic8800 国家码(country_code=00, custregd=Y)挡住，还是
#   (B) NetworkManager / 驱动 STA 扫描本身的问题。
#
# 用法 / Usage:  sudo ./wifi_diag.sh  [目标SSID]
# =============================================================================
set -u
TARGET="${1:-YOUR_WIFI_SSID_5G}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "$(id -u)" -ne 0 ]; then exec sudo "$0" "$@"; fi

echo "###########################################################"
echo "# WiFi 扫描诊断 / diag  —  目标 SSID: $TARGET"
echo "###########################################################"

echo; echo "== [0] 驱动国家码现状 / driver regdomain =="
echo "  country_code = $(cat /sys/module/aic8800_fdrv/parameters/country_code 2>/dev/null)"
echo "  custregd     = $(cat /sys/module/aic8800_fdrv/parameters/custregd 2>/dev/null)  (Y=忽略内核iw reg)"
iw reg get 2>/dev/null | grep -i country | sed 's/^/  kernel-reg: /'

echo; echo "== [1] 停 AP 服务 / stop AP =="
systemctl stop isc-dhcp-server isc-dhcp-server6 >/dev/null 2>&1
killall -9 hostapd wpa_supplicant >/dev/null 2>&1
nmcli device set wlan0 managed no >/dev/null 2>&1     # 让 iw 独占扫描，避免 NM 干扰
ip addr flush dev wlan0
ip link set wlan0 down; sleep 1; ip link set wlan0 up; sleep 2
iw reg set CN >/dev/null 2>&1

echo; echo "== [2] 底层 iw 扫描（绕过 NM）/ raw iw scan =="
SCAN=$(iw dev wlan0 scan 2>&1)
if echo "$SCAN" | grep -q 'command failed'; then
    echo "  !! iw 扫描失败: $(echo "$SCAN" | grep 'command failed')"
fi
TOTAL=$(echo "$SCAN" | grep -c '^BSS ')
echo "  扫到 AP 总数 / total APs = $TOTAL"

echo; echo "  --- 2.4GHz AP (freq 24xx) ---"
echo "$SCAN" | awk '/^BSS/{b=$2} /freq:/{f=$2} /SSID:/{s=substr($0,index($0,$2)); if(f>2400&&f<2500) printf "    %-6s %s\n", f, s}'
echo; echo "  --- 5GHz AP (freq 5xxx) ---"
G5=$(echo "$SCAN" | awk '/^BSS/{b=$2} /freq:/{f=$2} /SSID:/{s=substr($0,index($0,$2)); if(f>4900&&f<5900) printf "    %-6s %s\n", f, s}')
if [ -z "$G5" ]; then echo "    (无 5GHz AP 可见 — 若目标是5G，即被国家码挡住)"; else echo "$G5"; fi

echo; echo "== [3] 目标 \"$TARGET\" 是否可见 =="
if echo "$SCAN" | grep -qi "SSID: $TARGET"; then
    echo "$SCAN" | awk -v t="$TARGET" '/freq:/{f=$2} /signal:/{sig=$2} /SSID:/{if(index($0,t)) printf "  ✅ 找到！freq=%s MHz  signal=%s dBm\n", f, sig}'
    echo "  → 底层能扫到，问题在 NetworkManager；建议直接 wpa_cli/wpa_supplicant 连接。"
else
    echo "  ❌ 底层 iw 也扫不到 $TARGET"
    echo "     若上面 5GHz 列表为空且它是5G路由器 → 需改 aic8800 国家码(见下)。"
    echo "     若它是2.4G却扫不到 → 检查是否隐藏SSID/信号太弱/路由器已关。"
fi

echo; echo "== [4] 恢复 AP 模式 / restore AP =="
"$SCRIPT_DIR/wifi_ap.sh"
echo; echo "诊断结束。把上面 [2][3] 的结果发我。"
