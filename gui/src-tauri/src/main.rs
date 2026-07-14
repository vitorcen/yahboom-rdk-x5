#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::Command;

const BOARD: &str = "root@192.168.3.187";
const BOARD_IP: &str = "192.168.3.187";

// Whitelisted log sources -> the exact remote command we run for each.
// Never interpolate user input into the shell line.
const SERVICES: &[&str] = &[
    "ms200-lidar",
    "rosbridge",
    "mipi-cam",
    "nav-bringup",
    "nav2",
    "cam-service",
    "follow-me",
];

fn ssh(args: &[&str]) -> Result<String, String> {
    let out = Command::new("ssh")
        .args([
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=4",
            "-o", "StrictHostKeyChecking=accept-new",
            BOARD,
        ])
        .args(args)
        .output()
        .map_err(|e| format!("ssh spawn failed: {e}"))?;
    if out.status.success() {
        Ok(String::from_utf8_lossy(&out.stdout).into_owned())
    } else {
        let err = String::from_utf8_lossy(&out.stderr);
        Err(format!("ssh exit {}: {}", out.status, err.trim()))
    }
}

// All commands are async + spawn_blocking: a sync #[tauri::command] runs on the
// main thread, and a 1-4 s ssh there freezes the whole window.
async fn ssh_bg(args: Vec<String>) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let refs: Vec<&str> = args.iter().map(String::as_str).collect();
        ssh(&refs)
    })
    .await
    .map_err(|e| format!("task join failed: {e}"))?
}

fn v(args: &[&str]) -> Vec<String> {
    args.iter().map(|s| s.to_string()).collect()
}

// ---- Episode management ---------------------------------------------------

// Whitelist firewall for every command that takes an episode name.
// Name must be EXACTLY "ep_YYYYMMDD_HHMMSS" = "ep_" + 8 digits + "_" + 6 digits
// (18 bytes). This is the only shape allowed to reach a remote path or an
// export script argument, which structurally eliminates shell injection (no
// slashes, spaces, quotes, dashes, or `..` can pass). We have no `regex` crate,
// so this is the hand-written equivalent of ^ep_[0-9]{8}_[0-9]{6}$.
// Note: in-progress ".partial" recording dirs never match (the trailing
// ".partial" breaks the length/charset), so the UI can never export, pull, or
// delete a still-recording or truncated episode — that exclusion is deliberate.
fn valid_ep_name(name: &str) -> Result<(), String> {
    let b = name.as_bytes();
    let ok = b.len() == 18
        && &b[0..3] == b"ep_"
        && b[3..11].iter().all(u8::is_ascii_digit)
        && b[11] == b'_'
        && b[12..18].iter().all(u8::is_ascii_digit);
    if ok {
        Ok(())
    } else {
        Err(format!("invalid episode name (want ep_YYYYMMDD_HHMMSS): {name}"))
    }
}

// Local repo-side episodes root. No prior code locates the repo, so we anchor on
// the crate dir at compile time: gui/src-tauri -> up two -> repo root -> episodes.
fn local_episodes_dir() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(std::path::Path::parent)
        .map(|p| p.join("episodes"))
        .unwrap_or_else(|| std::path::PathBuf::from("episodes"))
}

// One ssh hop runs this stdlib-only python on the board (no third-party libs):
// walk /home/sunrise/episodes, parse each flat meta.yaml (our own "key: value"
// format, split on the first colon), du the dir, check preview/ existence, and
// read statvfs for free space. Emits one JSON line the Rust side then augments.
const EP_LIST_PY: &str = r#"python3 - <<'PYEOF'
import os, json, subprocess
root = "/home/sunrise/episodes"
eps = []
if os.path.isdir(root):
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        duration_s = None
        stopped_by = None
        meta = os.path.join(d, "meta.yaml")
        if os.path.isfile(meta):
            with open(meta) as f:
                for line in f:
                    if ":" not in line:
                        continue
                    k, _, val = line.partition(":")
                    k = k.strip(); val = val.strip()
                    if k == "duration_s":
                        try: duration_s = float(val)
                        except ValueError: pass
                    elif k == "stopped_by":
                        stopped_by = val or None
        size_mb = 0.0
        try:
            kb = int(subprocess.check_output(["du", "-sk", d]).split()[0])
            size_mb = round(kb / 1024.0, 1)
        except Exception:
            pass
        eps.append({
            "name": name,
            "partial": name.endswith(".partial"),
            "duration_s": duration_s,
            "stopped_by": stopped_by,
            "size_mb": size_mb,
            "preview_remote": os.path.isdir(os.path.join(d, "preview")),
        })
try:
    st = os.statvfs(root if os.path.isdir(root) else "/home/sunrise")
    disk_free_gb = round(st.f_bavail * st.f_frsize / (1024.0 ** 3), 1)
except Exception:
    disk_free_gb = 0.0
print(json.dumps({"disk_free_gb": disk_free_gb, "episodes": eps}))
PYEOF
"#;

#[derive(serde::Deserialize)]
struct BoardEp {
    name: String,
    partial: bool,
    duration_s: Option<f64>,
    stopped_by: Option<String>,
    size_mb: f64,
    preview_remote: bool,
}

#[derive(serde::Deserialize)]
struct BoardList {
    disk_free_gb: f64,
    episodes: Vec<BoardEp>,
}

#[derive(serde::Serialize)]
struct Ep {
    name: String,
    partial: bool,
    duration_s: Option<f64>,
    stopped_by: Option<String>,
    size_mb: f64,
    pulled: bool,
    // Absolute path of the local preview dir (frontend feeds it to
    // convertFileSrc), null when the preview has not been pulled yet.
    preview_local: Option<String>,
    preview_remote: bool,
}

#[derive(serde::Serialize)]
struct EpList {
    disk_free_gb: f64,
    episodes: Vec<Ep>,
}

/// Fetch journal for one whitelisted source. source = service name | "dmesg".
#[tauri::command]
async fn journal(source: String, lines: u32) -> Result<String, String> {
    let n = lines.clamp(10, 2000).to_string();
    if source == "dmesg" {
        return ssh_bg(v(&["dmesg", "--time-format", "iso", "|", "tail", "-n", &n])).await;
    }
    if !SERVICES.contains(&source.as_str()) {
        return Err(format!("unknown source: {source}"));
    }
    ssh_bg(v(&["journalctl", "-u", &source, "-n", &n, "--no-pager", "-o", "short-iso"])).await
}

/// One-shot status of all boot services: "name active\nname inactive..."
#[tauri::command]
async fn service_status() -> Result<String, String> {
    let list = SERVICES.join(" ");
    let script = format!(
        "for s in {list}; do printf '%s %s\\n' $s $(systemctl is-active $s); done"
    );
    ssh_bg(vec![script]).await
}

/// Board vitals shown in log sidebar footer: uptime + load + mem.
#[tauri::command]
async fn board_info() -> Result<String, String> {
    ssh_bg(v(&["uptime", "&&", "free", "-m", "|", "head", "-2"])).await
}

/// Dashboard gauges, one line each: SoC temp (m°C), loadavg, core count,
/// "used total" mem (MB), "used total" rootfs (MB).
/// The board has no current sensor, so power in watts is not measurable.
#[tauri::command]
async fn sysinfo() -> Result<String, String> {
    ssh_bg(v(&[
        "cat",
        "/sys/class/hwmon/hwmon0/temp1_input",
        "/proc/loadavg",
        ";",
        "nproc",
        ";",
        "free",
        "-m",
        "|",
        "awk",
        "'NR==2{print $3,$2}'",
        ";",
        "df",
        "-BM",
        "--output=used,size",
        "/",
        "|",
        "awk",
        "'NR==2{gsub(/M/,\"\"); print $1,$2}'",
    ]))
    .await
}

/// ICMP liveness probe — works even before sshd/rosbridge come up, so the UI
/// can show boot progress after a reboot. Returns "up" | "down".
#[tauri::command]
async fn alive() -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let ok = Command::new("ping")
            .args(["-c", "1", "-W", "1", BOARD_IP])
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false);
        Ok((if ok { "up" } else { "down" }).to_string())
    })
    .await
    .map_err(|e| format!("task join failed: {e}"))?
}

/// Teleop-chain self check: joystick device + every process in the
/// joy -> mux -> driver chain, via pgrep (fast, immune to the DDS-discovery
/// races that make `ros2 topic info` lie). One "name status" line each.
#[tauri::command]
async fn ctl_check() -> Result<String, String> {
    let script = r#"
p() { pgrep -f "$1" >/dev/null && echo OK || echo DEAD; }
echo "js0 $(test -e /dev/input/js0 && echo OK || echo MISSING)"
echo "joy_node $(p '[j]oy_node')"
echo "joy_ctrl $(p '[y]ahboom_joy')"
echo "driver $(p '[M]cnamu_driver')"
echo "mux $(p '[c]md_vel_mux.py')"
echo "app_conflict $(pgrep -f '[a]pp_SunriseRobot.py' >/dev/null && echo YES || echo no)"
echo "nav-bringup $(systemctl is-active nav-bringup) restarts=$(systemctl show nav-bringup -p NRestarts --value)"
"#;
    ssh_bg(vec![script.to_string()]).await
}

/// Restore the teleop chain: restart the chassis bringup service
/// (driver + joy + mux + ekf relaunch together).
#[tauri::command]
async fn ctl_fix() -> Result<String, String> {
    ssh_bg(v(&["systemctl", "restart", "nav-bringup"])).await
}

/// One "module up|down" line per diagram block on the system tab.
/// Read-only probes only; pgrep patterns use the [x] bracket trick so the
/// probe shell never matches itself.
#[tauri::command]
async fn mod_status() -> Result<String, String> {
    let script = r#"
echo "soc up"
echo "mem up"
echo "wifi $(ip -4 addr show wlan0 2>/dev/null | grep -qw inet && echo up || echo down)"
echo "cam $(systemctl -q is-active mipi-cam && echo up || echo down)"
echo "lidar $(systemctl -q is-active ms200-lidar && echo up || echo down)"
echo "joy $(test -e /dev/input/js0 && echo up || echo down)"
echo "chassis $(pgrep -f '[M]cnamu_driver' >/dev/null && echo up || echo down)"
echo "hub $(lsusb | grep -q '2109:0817' && echo up || echo down)"
echo "oled $(systemctl -q is-active yahboom_oled && echo up || echo down)"
echo "imu $(pgrep -f '[i]mu_filter' >/dev/null && echo up || echo down)"
"#;
    ssh_bg(vec![script.to_string()]).await
}

/// Per-module detail for the system tab: device nodes, services, processes.
/// Strict whitelist — the module name never reaches the shell line.
#[tauri::command]
async fn mod_info(module: String) -> Result<String, String> {
    let script = match module.as_str() {
        "soc" => r#"
echo "== 板卡 / 系统 =="
tr -d '\0' </sys/firmware/devicetree/base/model 2>/dev/null; echo
grep PRETTY_NAME /etc/os-release | cut -d= -f2 | tr -d '"'
echo "内核 $(uname -r)"
[ -f /etc/version ] && echo "RDK OS $(cat /etc/version)"
echo; echo "== CPU / BPU =="
echo "核数   $(nproc) × Cortex-A55 + 10TOPS BPU"
echo "负载   $(cut -d' ' -f1-3 /proc/loadavg)"
echo "主频   $(($(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq)/1000)) MHz"
echo "温度   $(($(cat /sys/class/hwmon/hwmon0/temp1_input)/1000)) °C"
echo; echo "== 运行时间 =="
uptime -p
"#,
        "mem" => r#"
echo "== 内存 =="
free -h | head -3
echo; echo "== 磁盘 =="
df -h | awk 'NR==1 || $6=="/" || $6~"userdata|boot"'
echo; echo "== 块设备 =="
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT 2>/dev/null | head -12
"#,
        "wifi" => r#"
echo "== 连接 =="
iw dev wlan0 link 2>/dev/null | sed 's/^\t/  /'
echo; echo "== 地址 =="
ip -4 addr show wlan0 | grep -w inet
echo; echo "== 驱动 aic8800（国家码是模块参数，见 modprobe.d）=="
cat /etc/modprobe.d/aic8800.conf 2>/dev/null
lsmod | grep -i aic | head -3
"#,
        "cam" => r#"
echo "== 传感器 =="
echo "Sony IMX219 · CSI0 · i2c-6 · 0x10 $(test -e /dev/i2c-6 && echo '(总线存在)' || echo '(总线缺失!)')"
echo; echo "== 设备节点（tros 走 vin/isc 通路，无 /dev/video* 属正常）=="
ls /dev/video* /dev/isc /dev/vin* 2>/dev/null | head -6 || true
echo; echo "== 服务（cam-service 是 ISP 中间件，必须 active）=="
for s in mipi-cam cam-service; do echo "$s: $(systemctl is-active $s)"; done
echo; echo "== 进程 =="
pgrep -af '[m]ipi_cam|[h]obot_codec|[w]ebsocket' | head -4
"#,
        "lidar" => r#"
echo "== USB 串口设备 =="
ls -l /dev/oradar* /dev/ttyACM* /dev/ttyUSB* 2>/dev/null || echo "无串口节点（USB 可能重枚举中）"
echo; echo "== 服务 =="
echo "ms200-lidar: $(systemctl is-active ms200-lidar) restarts=$(systemctl show ms200-lidar -p NRestarts --value)"
echo; echo "== 进程 =="
pgrep -af '[o]radar|[m]s200' | head -3
echo; echo "坑: USB 重枚举会让驱动假活——/scan 静默时重启 ms200-lidar 服务恢复"
"#,
        "joy" => r#"
echo "== 接收器 =="
ls -l /dev/input/js0 2>/dev/null || echo "/dev/input/js0 缺失（接收器没插或没配对）"
echo; echo "== 输入设备 =="
grep -E '^N: Name=' /proc/bus/input/devices | head -6
echo; echo "== 进程 =="
pgrep -af '[j]oy_node|[y]ahboom_joy' || echo "joy 节点未运行"
echo; echo "提示: 手柄有电但车不动 → 先按 SELECT/BACK 使能（Joy_active 开机默认关）"
"#,
        "chassis" => r#"
echo "== 串口 (RDK ↔ STM32，USB 转串) =="
ls -l /dev/myserial /dev/ttyUSB* 2>/dev/null | head -4
echo; echo "== 服务 =="
echo "nav-bringup: $(systemctl is-active nav-bringup) restarts=$(systemctl show nav-bringup -p NRestarts --value)"
echo; echo "== 进程链 =="
pgrep -af '[M]cnamu_driver|[c]md_vel_mux|[e]kf_node' | head -5
echo; echo "== 最近日志 =="
journalctl -u nav-bringup -n 6 --no-pager -o cat | tail -6
"#,
        "hub" => r#"
echo "== HUB 芯片（扩展板 = USB3.0 + USB2.0 双芯片，一板双面）=="
lsusb | grep -iE 'hub'
echo; echo "== USB 拓扑（谁挂在谁下面、协商速率）=="
lsusb -t
echo; echo "== 挂载的外设 =="
lsusb | grep -ivE 'hub|root'
"#,
        "oled" => r#"
echo "== 屏幕 =="
echo "0.91\" SSD1306 128×32 · i2c-5 · 0x3C $(test -e /dev/i2c-5 && echo '(总线存在)')"
echo "接法: RDK 40pin 排针 → 风扇扩展板顺延 → OLED 排线插在角落引脚"
echo; echo "== 服务 =="
echo "yahboom_oled: $(systemctl is-active yahboom_oled)"
echo; echo "== 进程（app_SunriseRobot/oled.py，轮询显示 IP/CPU/内存等）=="
pgrep -af '[o]led.py' | head -2
echo; echo "== 程序 / 配置 =="
echo "unit:   $(systemctl show yahboom_oled -p FragmentPath --value)"
echo "程序:   /home/sunrise/sunriseRobot/app_SunriseRobot/oled.py（显示内容改这里）"
echo; echo "== 最近日志 =="
journalctl -u yahboom_oled -n 5 --no-pager -o cat | tail -5
"#,
        "imu" => r#"
echo "== 数据链 =="
echo "九轴 IMU（底盘扩展板）→ STM32 → /dev/myserial → Mcnamu_driver"
echo "→ /imu/data_raw → imu_filter_madgwick 姿态融合 → /imu/data → ekf_node 里程计融合"
echo; echo "== 进程 =="
pgrep -af '[i]mu_filter|[e]kf_node' | grep -v 'bash -c' | head -3
echo; echo "== 实测频率（采样约 5 s，请稍候）=="
export ROS_DOMAIN_ID=99; source /opt/tros/humble/setup.bash 2>/dev/null
timeout 6 ros2 topic hz /imu/data_raw --window 20 2>/dev/null | grep -m1 'average rate' || echo "(无数据——驱动可能没起)"
"#,
        _ => return Err(format!("unknown module: {module}")),
    };
    ssh_bg(vec![script.to_string()]).await
}

/// Shell helper prepended to every stack_info script: `u <service>` prints the
/// systemd unit path + its ExecStart line, looked up live so it never lies.
const UNIT_HELPER: &str = r#"u() { echo "unit:   $(systemctl show $1 -p FragmentPath --value)"; systemctl cat $1 2>/dev/null | grep -m1 '^ExecStart=' | sed 's/^ExecStart=/启动:   /' | cut -c1-320; }
pg() { r=$(pgrep -af "$1" | grep -v 'bash -c' | head -3); [ -n "$r" ] && echo "$r" || echo "${2:-未运行}"; }
"#;

/// Per-node detail for the ROS2 stack diagram: process cmdline, program +
/// config paths, live topic rate where it proves the node's output, service
/// journal. Strict whitelist.
#[tauri::command]
async fn stack_info(node: String) -> Result<String, String> {
    let script = match node.as_str() {
        "overview" => r#"
echo "== systemd 自启服务 =="
for s in ms200-lidar rosbridge mipi-cam nav-bringup nav2 cam-service; do printf '%-12s %s\n' $s $(systemctl is-active $s); done
echo; echo "== 节点按出身（对照左图颜色）=="
echo "上游:  Nav2 容器 / madgwick / ekf_node / joy_node / rosbridge / oradar(厂商)"
echo "tros:  mipi_cam / hobot_codec / websocket(:8000)"
echo "亚博:  Mcnamu_driver / base_node(轮速→odom_raw) / joy_ctrl（oled.py 在栈外）"
echo "自研:  cmd_vel_mux（速度仲裁）/ 本 GUI"
echo; echo "== 程序 / 配置在哪 =="
echo "unit 文件:  /etc/systemd/system/*.service（仓库 board/ 目录 1:1 镜像）"
echo "亚博工作区: /home/sunrise/yahboomcar_ws/install/"
echo "自研脚本:   /home/sunrise/nav_config/ · 地图: /home/sunrise/maps/"
echo "tros:      /opt/tros/humble/ · ROS2 上游: /opt/ros/humble/"
echo; echo "ROS2 Humble · tros 2.3.0 · ROS_DOMAIN_ID=99"
"#,
        "rosbridge" => r#"
echo "== 服务 =="
echo "rosbridge: $(systemctl is-active rosbridge)"
echo; echo "== 9090 端口在线客户端 =="
ss -Htn state established '( sport = :9090 )' 2>/dev/null | awk '{print "  "$4" ← "$5}' | sort -u
echo; echo "== 进程 =="
pg '[r]osbridge_websocket'
echo; echo "== 程序 / 配置 =="
u rosbridge
echo "程序:   /opt/ros/humble/lib/rosbridge_server/rosbridge_websocket"
echo "launch: rosbridge_server/rosbridge_websocket_launch.xml（端口等参数在此）"
echo; echo "== 最近日志 =="
journalctl -u rosbridge -n 4 --no-pager -o cat | tail -4
"#,
        "webview" => r#"
echo "== 8000 端口 =="
ss -tlnp 2>/dev/null | grep ':8000 ' || echo "8000 未监听"
echo; echo "== 进程（tros websocket，随 mipi-cam 服务拉起）=="
pg '[w]ebsocket'
echo; echo "== 程序 / 配置 =="
echo "程序:   /opt/tros/humble/lib/websocket/websocket"
echo "配置:   /opt/tros/humble/lib/websocket/config/（前端页面资源同目录）"
echo; echo "浏览器入口: http://192.168.3.187:8000/TogetheROS/"
"#,
        "nav2" => r#"
echo "== 服务 =="
echo "nav2: $(systemctl is-active nav2) restarts=$(systemctl show nav2 -p NRestarts --value)"
echo; echo "== 用的地图和调参文件 =="
pgrep -af '[n]avigation_dwb_launch' | head -1 | tr ' ' '\n' | grep -E '^(map|params_file):='
echo; echo "== 程序 / 配置 =="
u nav2
echo "launch: yahboomcar_nav/launch/navigation_dwb_launch.py（yahboomcar_ws）"
echo; echo "== 容器（amcl/planner/controller/bt_nav 组合在一个进程里）=="
systemd-cgls -u nav2.service --no-pager 2>/dev/null | head -6
echo; echo "== 最近日志 =="
journalctl -u nav2 -n 4 --no-pager -o cat | tail -4
"#,
        "madgwick" => r#"
echo "== 进程 =="
pg '[i]mu_filter_madgwick'
echo; echo "== 职责 =="
echo "订 /imu/data_raw（加速度+角速度）→ Madgwick 解算姿态四元数 → 发 /imu/data"
echo "注: use_mag=false——九轴的磁力计 /imu/mag 有发布但融合没用（源码核实）"
echo; echo "== 程序 / 配置 =="
echo "程序:   /opt/ros/humble/lib/imu_filter_madgwick/imu_filter_madgwick_node"
echo "参数:   $(pgrep -af '[i]mu_filter_madgwick' | grep -v 'bash -c' | grep -oE -- '--params-file [^ ]+' | head -1 | cut -d' ' -f2)"
echo "随 nav-bringup 服务拉起："; u nav-bringup
echo; echo "== 输出 /imu/data 实测（采样约 5 s）=="
export ROS_DOMAIN_ID=99; source /opt/tros/humble/setup.bash 2>/dev/null
timeout 6 ros2 topic hz /imu/data --window 20 2>/dev/null | grep -m1 'average rate' || echo "(无数据)"
"#,
        "ekf" => r#"
echo "== 进程 =="
pg '[e]kf_node'
echo; echo "== 职责 =="
echo "robot_localization EKF：融合 /imu/data + /odom_raw（base_node 由轮速 /vel_raw 积分）"
echo "→ /odom（重映射自 /odometry/filtered）"
echo; echo "== 程序 / 配置 =="
echo "程序:   /opt/ros/humble/lib/robot_localization/ekf_node"
echo "参数:   $(pgrep -af '[e]kf_node' | grep -v 'bash -c' | grep -oE -- '--params-file [^ ]+' | head -1 | cut -d' ' -f2)"
echo "随 nav-bringup 服务拉起："; u nav-bringup
echo; echo "== 输出 /odom 实测（采样约 5 s）=="
export ROS_DOMAIN_ID=99; source /opt/tros/humble/setup.bash 2>/dev/null
timeout 6 ros2 topic hz /odom --window 20 2>/dev/null | grep -m1 'average rate' || echo "(无数据)"
"#,
        "joyctrl" => r#"
echo "== 进程 =="
pg '[y]ahboom_joy'
echo; echo "== 职责 =="
echo "亚博键位映射：订 /joy → 发 /cmd_vel_joy（mux 高优先级）"
echo "SELECT(buttons[6]) 切换 Joy_active 使能并同时发零速刹停（开机默认关——"
echo "手柄有电车不动先按这个）；另有 RGB 灯/蜂鸣/线速角速档位键"
echo; echo "== 程序 / 配置 =="
echo "程序:   /home/sunrise/yahboomcar_ws/install/yahboomcar_ctrl/lib/yahboomcar_ctrl/yahboom_joy"
echo "源码包: yahboomcar_ctrl（键位/速度系数在包内源码，无独立配置文件）"
echo "随 nav-bringup 服务拉起："; u nav-bringup
echo; echo "== 底盘服务日志中的 joy 记录 =="
journalctl -u nav-bringup -n 300 --no-pager -o cat 2>/dev/null | grep -i 'joy' | tail -4
"#,
        "mux" => r#"
echo "== 进程 =="
pg '[c]md_vel_mux'
echo; echo "== 仲裁规则（自研，含看门狗）=="
echo "/cmd_vel_joy（手柄，高） > /cmd_vel（Nav2/键盘，低） → /cmd_vel_drv"
echo "手柄动作期间压制导航与键盘；源头静默由看门狗放行低优先级"
echo; echo "== 程序 / 配置 =="
echo "程序:   /home/sunrise/nav_config/cmd_vel_mux.py（仓库 board/ 有镜像）"
echo "配置:   无独立文件——优先级/看门狗阈值写在脚本头部常量里"
echo "随 nav-bringup 服务拉起："; u nav-bringup
echo; echo "== 输出 /cmd_vel_drv 实测（采样约 5 s；静止时无数据属正常）=="
export ROS_DOMAIN_ID=99; source /opt/tros/humble/setup.bash 2>/dev/null
timeout 6 ros2 topic hz /cmd_vel_drv --window 20 2>/dev/null | grep -m1 'average rate' || echo "(静止，无输出)"
"#,
        "mipicam" => r#"
echo "== 服务（cam-service 是 ISP 中间件，必须 active）=="
for s in mipi-cam cam-service; do echo "$s: $(systemctl is-active $s)"; done
echo; echo "== 进程链（采图 → 硬编码 JPEG → 分发）=="
pg '[m]ipi_cam|[h]obot_codec'
echo; echo "== 程序 / 配置 =="
u mipi-cam
echo "程序:   /opt/tros/humble/lib/mipi_cam/mipi_cam + hobot_codec"
echo "launch: mipi_cam/mipi_cam_websocket.launch.py（分辨率/编码参数在此）"
echo; echo "== 最近日志 =="
journalctl -u mipi-cam -n 4 --no-pager -o cat | tail -4
"#,
        "oradarnode" => r#"
echo "== 服务 =="
echo "ms200-lidar: $(systemctl is-active ms200-lidar) restarts=$(systemctl show ms200-lidar -p NRestarts --value)"
echo; echo "== 进程 =="
pg '[o]radar_scan' | cut -c1-160
echo; echo "== 程序 / 配置 =="
u ms200-lidar
echo "程序:   /home/sunrise/software/library_ws/install/oradar_lidar/lib/oradar_lidar/oradar_scan"
echo "launch: oradar_lidar/ms200_scan.launch.py（串口/角度等参数在此，启动时展开为 /tmp/launch_params_*）"
echo; echo "== 输出 /scan 实测（采样约 5 s）=="
export ROS_DOMAIN_ID=99; source /opt/tros/humble/setup.bash 2>/dev/null
timeout 6 ros2 topic hz /scan --window 20 2>/dev/null | grep -m1 'average rate' || echo "(无数据——USB 重枚举假活？重启 ms200-lidar 可恢复)"
"#,
        "joynode" => r#"
echo "== 进程 =="
pg '[j]oy_node'
echo "js0: $(test -e /dev/input/js0 && echo 存在 || echo 缺失)"
echo; echo "== 职责 =="
echo "ROS2 上游 joy 包：读 /dev/input/js0 → 发布 /joy（按钮+摇杆原始值）"
echo; echo "== 程序 / 配置 =="
echo "程序:   /opt/ros/humble/lib/joy/joy_node（上游 apt 包，无独立配置）"
echo "随 nav-bringup 服务拉起："; u nav-bringup
"#,
        "driver" => r#"
echo "== 进程 =="
pg '[M]cnamu_driver' '未运行（RGB I2C 崩溃重生间隙？欠压时高发）'
echo; echo "== 职责 =="
echo "亚博底盘协议：订 /cmd_vel_drv → STM32 串口下发（另订 RGBLight/Buzzer）"
echo "回读发布: /imu/data_raw /imu/mag(磁力计) /vel_raw(轮速) /voltage /edition"
echo "/vel_raw → base_node(C++,亚博) 积分成 /odom_raw → ekf 融合"
echo; echo "== 程序 / 配置 =="
echo "程序:   /home/sunrise/yahboomcar_ws/install/yahboomcar_bringup/lib/yahboomcar_bringup/Mcnamu_driver"
echo "源码包: yahboomcar_bringup（串口协议/车型参数在包内源码）"
echo "随 nav-bringup 服务拉起："; u nav-bringup
echo; echo "== 底盘服务近期报错（I2C 报错密集 = 欠压前兆）=="
journalctl -u nav-bringup -n 300 --no-pager -o cat 2>/dev/null | grep -iE 'error|i2c|121' | tail -5
"#,
        "navapp" => r#"
echo "== 是什么 =="
echo "亚博建图导航 APP（Android）——和本 GUI 一样是 rosbridge ws:9090 客户端"
echo; echo "== 用到的接口 =="
echo "订阅: /map（栅格地图）· /robot_pose（robot_pose_publisher_ros2 把 TF"
echo "      转成普通话题，APP 端不用解 TF）"
echo "发布: /goal_pose /initialpose /cmd_vel（虚拟摇杆，走 mux 低优先级）"
echo "服务: /yahboomAppSaveMap（WebSaveMap.srv——板端收到后跑 nav2 map_saver_cli 存图）"
echo; echo "== 配套板端节点当前状态 =="
pg '[r]obot_pose_publisher|[s]ave_map|[y]ahboom_app_save' '未运行（跟建图 launch 一起才拉起，纯导航时不需要）'
echo; echo "== 程序 / 配置 =="
echo "板端包: yahboomcar_ws/install/{yahboom_app_save_map, yahboom_web_savmap_interfaces, robot_pose_publisher_ros2}"
echo "与本 GUI 走同一 rosbridge，功能重叠，可同时在线"
"#,
        "phoneapp" => r#"
echo "== 是什么 =="
echo "亚博手机遥控 APP（Android/iOS）→ 板端 app_SunriseRobot.py 服务端"
echo "TCP :6000 控制协议（struct 打包）· HTTP :6500 MJPEG 视频（Flask/gevent）"
echo; echo "== 与 ROS 栈的关系：完全绕过 =="
echo "app_SunriseRobot.py 用 SunriseRobotLib 直写 /dev/myserial → STM32，"
echo "与 Mcnamu_driver 抢串口、并独占 CSI0 相机——所以已禁自启"
echo "（🎮 遥控自检里的 app_conflict 查的就是它）"
echo; echo "== 当前状态 =="
r=$(pgrep -af '[a]pp_SunriseRobot.py' | grep -vE 'oled|bash -c' | head -2); [ -n "$r" ] && echo "$r" || echo "未运行（正常——与 ROS 栈互斥）"
echo "监听: $(ss -tln 2>/dev/null | grep -cE ':6000 |:6500 ') 个端口在听（0=未启动）"
echo; echo "== 程序 / 配置 =="
echo "程序: /home/sunrise/sunriseRobot/app_SunriseRobot/app_SunriseRobot.py"
echo "要用它: 停 nav-bringup 后手动 start_app.sh（或 hybrid 模式保留 6000 遥控）"
"#,
        _ => return Err(format!("unknown node: {node}")),
    };
    ssh_bg(vec![format!("{UNIT_HELPER}{script}")]).await
}

/// Follow-me switch state: "active enabled" | "inactive disabled" | ...
/// (echo wrapper: is-active exits nonzero when inactive, which ssh() treats
/// as an error).
#[tauri::command]
async fn follow_get() -> Result<String, String> {
    ssh_bg(vec![
        "echo $(systemctl is-active follow-me) $(systemctl is-enabled follow-me 2>/dev/null)"
            .into(),
    ])
    .await
}

/// Toggle follow-me. enable/disable --now persists the choice in systemd,
/// so the state survives board reboots.
#[tauri::command]
async fn follow_set(on: bool) -> Result<String, String> {
    let cmd = if on {
        "systemctl enable --now follow-me"
    } else {
        "systemctl disable --now follow-me"
    };
    ssh_bg(vec![format!("{cmd} && echo ok")]).await
}

/// Mapping mode state: "active" | "inactive" | ... (echo wrapper as above).
#[tauri::command]
async fn mapping_get() -> Result<String, String> {
    ssh_bg(vec!["echo $(systemctl is-active mapping)".into()]).await
}

/// Toggle mapping mode. start/stop only — never enabled: mapping must not
/// survive a reboot. Conflicts= in the unit swaps nav2 out automatically on
/// start; on stop we bring nav2 back explicitly (Conflicts is not a Wants).
#[tauri::command]
async fn mapping_set(on: bool) -> Result<String, String> {
    // Fire-and-forget on the board: the nav2<->mapping handover takes ~20 s
    // and blocking the ssh (and the GUI click) on it feels dead. The GUI
    // polls mapping_get until the target state settles.
    // NB: the off-branch ends in '&' — nothing may be appended after it
    // ('&; echo' is a bash syntax error that silently no-ops the whole line).
    let cmd = if on {
        "systemctl reset-failed mapping 2>/dev/null; systemctl start --no-block mapping; echo ok"
    } else {
        "nohup bash -c 'systemctl stop mapping; systemctl reset-failed nav2 2>/dev/null; systemctl start nav2' >/dev/null 2>&1 & echo ok"
    };
    ssh_bg(vec![cmd.to_string()]).await
}

/// Save the live cartographer map over room.{yaml,pgm} (one .bak generation),
/// stop mapping and restart nav2 on the new map. ~10-20 s.
#[tauri::command]
async fn map_save() -> Result<String, String> {
    ssh_bg(vec!["bash /home/sunrise/nav_config/map_save.sh".into()]).await
}

/// Reboot / power off the board. Action is a strict whitelist, and the UI
/// arms the button first (two clicks) so a stray click can't cut power.
#[tauri::command]
async fn power(action: String) -> Result<String, String> {
    match action.as_str() {
        "reboot" => ssh_bg(v(&["systemctl", "reboot"])).await,
        "poweroff" => ssh_bg(v(&["systemctl", "poweroff"])).await,
        _ => Err(format!("unknown action: {action}")),
    }
}

/// List episodes on the board, merged with local pull state. Returns the JSON
/// contract {disk_free_gb, episodes:[{name, partial, duration_s, stopped_by,
/// size_mb, pulled, preview_local, preview_remote}]}. The board half comes from
/// EP_LIST_PY; pulled/preview_local are decided here from the local repo tree.
/// Read-only — no name reaches the shell (EP_LIST_PY takes no argument).
#[tauri::command]
async fn ep_list() -> Result<String, String> {
    let raw = ssh_bg(vec![EP_LIST_PY.to_string()]).await?;
    let board: BoardList = serde_json::from_str(raw.trim())
        .map_err(|e| format!("parse board json failed: {e}: {raw}"))?;
    let base = local_episodes_dir();
    let episodes: Vec<Ep> = board
        .episodes
        .into_iter()
        .map(|e| {
            let dir = base.join(&e.name);
            let preview = dir.join("preview");
            Ep {
                pulled: dir.join("bag").is_dir(),
                preview_local: preview
                    .is_dir()
                    .then(|| preview.to_string_lossy().into_owned()),
                name: e.name,
                partial: e.partial,
                duration_s: e.duration_s,
                stopped_by: e.stopped_by,
                size_mb: e.size_mb,
                preview_remote: e.preview_remote,
            }
        })
        .collect();
    serde_json::to_string(&EpList { disk_free_gb: board.disk_free_gb, episodes })
        .map_err(|e| format!("serialize failed: {e}"))
}

/// Ask the board to bag-export one episode (idempotent — the script skips if
/// already done). `name` is whitelist-validated before it is interpolated, so
/// only ep_YYYYMMDD_HHMMSS can ever reach the shell line. Runs on the blocking
/// pool via ssh_bg, so a slow export never freezes the UI thread.
#[tauri::command]
async fn ep_export(name: String, preview: bool) -> Result<String, String> {
    valid_ep_name(&name)?;
    let mut cmd = format!(
        "source /opt/tros/humble/setup.bash && \
         python3 /home/sunrise/nav_config/episode_export.py {name}"
    );
    if preview {
        cmd.push_str(" --preview");
    }
    ssh_bg(vec![cmd]).await
}

/// Pull one episode from the board into the local repo episodes/<name>/ via
/// rsync. preview=true fetches only meta.yaml + preview/ (include-rules,
/// everything else excluded); false mirrors the whole dir. `name` is
/// whitelist-validated first, so the remote path is a fixed, injection-proof
/// ep_YYYYMMDD_HHMMSS. Blocking rsync runs on spawn_blocking off the UI thread.
#[tauri::command]
async fn ep_pull(name: String, preview: bool) -> Result<String, String> {
    valid_ep_name(&name)?;
    let dest = local_episodes_dir().join(&name);
    tauri::async_runtime::spawn_blocking(move || {
        std::fs::create_dir_all(&dest)
            .map_err(|e| format!("mkdir {} failed: {e}", dest.display()))?;
        // Reuse the same ssh hardening as ssh() for the rsync transport.
        let mut args: Vec<String> = vec![
            "-a".into(),
            "--info=stats2".into(),
            "-e".into(),
            "ssh -o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=accept-new"
                .into(),
        ];
        if preview {
            // Only meta.yaml + the whole preview/ subtree; drop the bag/raw data.
            args.push("--include=meta.yaml".into());
            args.push("--include=preview/".into());
            args.push("--include=preview/**".into());
            args.push("--exclude=*".into());
        }
        // name is validated ep_YYYYMMDD_HHMMSS: no metachars can reach the path.
        args.push(format!("{BOARD}:/home/sunrise/episodes/{name}/"));
        args.push(format!("{}/", dest.display()));
        let out = Command::new("rsync")
            .args(&args)
            .output()
            .map_err(|e| format!("rsync spawn failed: {e}"))?;
        if !out.status.success() {
            let err = String::from_utf8_lossy(&out.stderr);
            return Err(format!("rsync exit {}: {}", out.status, err.trim()));
        }
        // Return the last few lines (rsync's transfer/stats summary).
        let stdout = String::from_utf8_lossy(&out.stdout);
        let lines: Vec<&str> = stdout.lines().collect();
        let start = lines.len().saturating_sub(8);
        Ok(lines[start..].join("\n"))
    })
    .await
    .map_err(|e| format!("task join failed: {e}"))?
}

/// Delete one episode directory on the board. SAFETY: `name` must pass the
/// ep_YYYYMMDD_HHMMSS whitelist — anything else returns Err before any shell
/// runs, so injection is eliminated at the root. The path is fixed and `rm -rf
/// --` stops a name from being read as an option. ".partial" dirs fail the
/// whitelist by design (no deleting in-progress/truncated recordings). The
/// two-step confirm is the frontend's job.
#[tauri::command]
async fn ep_delete(name: String) -> Result<String, String> {
    valid_ep_name(&name)?;
    ssh_bg(vec![format!(
        "rm -rf -- /home/sunrise/episodes/{name} && echo deleted {name}"
    )])
    .await
}

/// Open a pulled episode in a local RViz replay (scripts/replay.sh: bag play
/// on ROS_DOMAIN_ID=42 + rviz2, both die when the RViz window closes).
/// Detached — the GUI does not babysit the viewer.
#[tauri::command]
async fn ep_replay(name: String) -> Result<String, String> {
    valid_ep_name(&name)?;
    let ep = local_episodes_dir().join(&name);
    if !ep.join("bag").is_dir() {
        return Err("本地无完整 bag,先拉取完整".into());
    }
    let script = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(2)
        .ok_or("repo root not found")?
        .join("scripts/replay.sh");
    std::process::Command::new(script)
        .arg(&ep)
        .spawn()
        .map_err(|e| format!("spawn replay failed: {e}"))?;
    Ok(format!("replay started for {name}"))
}

fn main() {
    tauri::Builder::default()
        // single instance: a second launch just focuses the existing window
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            use tauri::Manager;
            if let Some(w) = app.webview_windows().values().next() {
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }))
        .invoke_handler(tauri::generate_handler![
            journal,
            service_status,
            board_info,
            sysinfo,
            power,
            alive,
            ctl_check,
            ctl_fix,
            mod_status,
            mod_info,
            stack_info,
            follow_get,
            follow_set,
            mapping_get,
            mapping_set,
            map_save,
            ep_list,
            ep_export,
            ep_pull,
            ep_delete,
            ep_replay
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
