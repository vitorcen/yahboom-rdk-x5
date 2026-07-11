// Assembly: tabs + connect wiring, board power buttons, host liveness pill.
// Importing a module registers its topic handlers and DOM listeners
// (side-effect modules by design).
import { $, S, invoke } from './state.js';
import { connect } from './ros.js';
import './viewer.js';
import './camera.js';
import './health.js';
import { keyStop } from './teleop.js';
import { onLogsShown } from './logs.js';

document.querySelectorAll('.tab').forEach(b => b.onclick = () => {
  S.page = b.dataset.page;
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x === b));
  $('page-dash').classList.toggle('on', S.page === 'dash');
  $('page-logs').classList.toggle('on', S.page === 'logs');
  if (S.page === 'logs') { keyStop(); onLogsShown(); }
});

// ---- board power buttons: first click arms (red, 3 s), second click fires ----
// After firing, the host pill runs a state machine: systemd takes ~1 min to
// stop the Nav2 stack before the board actually drops, so plain ping keeps
// answering "up" — we must show "重启中" until we see down, then up again.
let powerPending = null;   // null | 'reboot' | 'poweroff'
let seenDown = false;
function armPower(btn, label, action, doneMsg) {
  let armTimer = null;
  btn.onclick = async () => {
    if (!armTimer) {
      btn.textContent = '确认' + label.slice(2) + '?'; btn.classList.add('arm');
      armTimer = setTimeout(() => {
        armTimer = null; btn.textContent = label; btn.classList.remove('arm');
      }, 3000);
      return;
    }
    clearTimeout(armTimer); armTimer = null;
    btn.textContent = label; btn.classList.remove('arm');
    const st = $('st');
    try {
      await invoke('power', { action });
      st.textContent = doneMsg; st.className = 'pill warn';
      powerPending = action; seenDown = false;
      pollAlive();
    } catch (e) { st.textContent = '电源命令失败: ' + e; st.className = 'pill bad'; }
  };
}

// ---- host liveness: ping probe, independent of rosbridge ----
async function pollAlive() {
  const el = $('host');
  el.style.display = 'inline';
  let up;
  try { up = (await invoke('alive')) === 'up'; }
  catch { el.textContent = '主机 ?'; el.className = 'pill'; return; }
  if (powerPending) {
    if (!up) seenDown = true;
    if (powerPending === 'poweroff' && seenDown) {
      powerPending = null;
      el.textContent = '主机已关机'; el.className = 'pill bad'; return;
    }
    if (powerPending === 'reboot' && seenDown && up) {
      powerPending = null;   // down -> up transition: reboot finished
    } else {
      el.textContent = seenDown
        ? (powerPending === 'reboot' ? '重启中（已掉线）…' : '关机中…')
        : (powerPending === 'reboot' ? '重启中（停服务约 1 min）…' : '关机中（停服务约 1 min）…');
      el.className = 'pill warn'; return;
    }
  }
  el.textContent = up ? '主机在线' : '主机离线';
  el.className = 'pill ' + (up ? 'ok' : 'bad');
}

if (invoke) {
  armPower($('pReboot'), '⟳ 重启', 'reboot', '已发送重启，等待主机回来…');
  armPower($('pOff'),    '⏻ 关机', 'poweroff', '已发送关机（开机需按板上电源键）');
  pollAlive(); setInterval(pollAlive, 3000);
} else {
  $('power').style.display = 'none';   // browser mode: no ssh backend
}

$('btn').onclick = connect;
connect();
