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
            ctl_fix
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
