// System tab: one merged SVG on the left (hardware topology + inverted ROS2
// stack), a single detail panel on the right serving both hardware modules
// (mod_info) and stack nodes (stack_info). Status dots poll every 10 s.
import { $, S, invoke } from './state.js';

const TITLE = {
  soc: 'SoC — 地平线 Sunrise X5', mem: '内存 / 存储', wifi: 'WiFi — aic8800D80',
  cam: '相机 — Sony IMX219', lidar: '雷达 — ORADAR MS200', joy: '手柄接收器',
  chassis: '底盘扩展板 — STM32（亚博）',
  hub: 'USB3 HUB 扩展板 — VIA Labs', oled: 'OLED 屏 — SSD1306 128×32',
  imu: '九轴 IMU（经底盘串口）',
};
const STITLE = {
  overview: '软件栈总览', rosbridge: 'rosbridge — WebSocket 桥（上游）',
  gui: '本 GUI — Tauri（自研）', webview: 'Web 预览 :8000 — TogetheROS（tros）',
  nav2: 'Nav2 组合容器（上游）', madgwick: 'imu_filter_madgwick — 姿态融合（上游）',
  ekf: 'ekf_node — robot_localization（上游）', joyctrl: 'joy_ctrl — 键位映射（亚博）',
  mux: 'cmd_vel_mux — 速度仲裁（自研）', mipicam: 'mipi_cam · hobot_codec（tros）',
  oradarnode: 'oradar 雷达驱动（厂商）', joynode: 'joy_node — 手柄读取（上游）',
  driver: 'Mcnamu_driver — 底盘协议（亚博）',
  navapp: '建图导航 APP — rosbridge 客户端（亚博）',
  phoneapp: '手机遥控 APP — TCP 直连，绕过 ROS（亚博）',
};
// The GUI node runs on this machine, not the board — static description.
const GUI_TEXT = `== 就是本程序 ==
Tauri 2：Rust 后端 + 系统 WebView，前端零依赖 ES 模块。

== 与小车只有两条通道 ==
rosbridge ws://…:9090   实时数据与控制（雷达/地图/相机/导航/遥控）
ssh :22（免密公钥）      日志、服务状态、本页所有探测、电源命令

== 发布的话题 ==
/goal_pose（拖线导航目标） · /cmd_vel（键盘遥控，mux 低优先级）

== 程序 / 配置 ==
源码: 仓库 gui/（src-tauri/ Rust 后端 + ui/ 前端，编译时嵌入二进制）
启动: gui/run.sh（一键编译+运行） · 无运行期配置文件`;

let cur = null;   // { kind: 'mod' | 'node', id }
async function show(kind, id) {
  cur = { kind, id };
  document.querySelectorAll('#sysdiag .modblk').forEach(g =>
    g.classList.toggle('sel', kind === 'mod' && g.dataset.mod === id));
  document.querySelectorAll('#sysdiag .stkblk').forEach(g =>
    g.classList.toggle('sel', kind === 'node' && g.dataset.node === id));
  $('sysTitle').textContent = (kind === 'mod' ? TITLE[id] : STITLE[id]) || id;
  const out = $('sysOut');
  if (kind === 'node' && id === 'gui') { out.textContent = GUI_TEXT; return; }
  if (!invoke) { out.textContent = '详情需要桌面端（ssh 后端）。'; return; }
  out.textContent = '读取中…';
  const me = cur;
  try {
    const txt = await invoke(kind === 'mod' ? 'mod_info' : 'stack_info',
                             kind === 'mod' ? { module: id } : { node: id });
    if (cur === me) out.textContent = txt.trim();   // drop stale responses
  } catch (e) { if (cur === me) out.textContent = '读取失败: ' + e; }
}

document.querySelectorAll('#sysdiag .modblk').forEach(g =>
  g.addEventListener('click', () => show('mod', g.dataset.mod)));
document.querySelectorAll('#sysdiag .stkblk').forEach(g =>
  g.addEventListener('click', () => show('node', g.dataset.node)));
// section headers: board title -> SoC detail, stack title -> overview
document.querySelectorAll('#sysdiag .hdrclk').forEach(t =>
  t.addEventListener('click', () =>
    t.dataset.node ? show('node', t.dataset.node) : show('mod', t.dataset.mod)));
$('sysRefresh').onclick = () => { if (cur) show(cur.kind, cur.id); pollStatus(); };

async function pollStatus() {
  if (!invoke) return;
  try {
    for (const l of (await invoke('mod_status')).trim().split('\n')) {
      const [m, st] = l.split(' ');
      const el = $('md-' + m);
      if (el) el.setAttribute('class', 'st ' + (st || ''));
    }
  } catch { /* board unreachable: dots keep last state */ }
}

let timer = null;
export function onSysShown() {
  pollStatus();
  if (!timer) timer = setInterval(() => { if (S.page === 'sys') pollStatus(); }, 10000);
  if (!cur) show('node', 'overview');
}
