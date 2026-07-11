#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::Command;

const BOARD: &str = "root@192.168.3.187";

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

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![journal, service_status, board_info])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
