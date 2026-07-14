// Instrument bar: battery gauge, SoC temp + CPU load (ssh, Tauri only),
// topic-freshness dots. The board has no current sensor, so real power (W)
// is unreadable — temp/CPU stand in as the load indicators.
import { $, invoke } from './state.js';
import { onTopic, connected, send, cancelAllGoals } from './ros.js';

// ---- topic freshness -> dots ----
const last = {};                                  // topic -> performance.now()
let gotMap = false;                               // /map is latched: sent once on subscribe
for (const t of ['/scan', '/map', '/image_jpeg', '/plan'])
  onTopic(t, () => { last[t] = performance.now(); if (t === '/map') gotMap = true; });

const dot = (id, cls) => { $(id).firstElementChild.className = 'dot' + (cls ? ' ' + cls : ''); };
setInterval(() => {
  const now = performance.now();
  const fresh = (t, ms) => connected() && now - (last[t] || 0) < ms;
  dot('iScan', fresh('/scan', 2000) && 'up');
  dot('iMap',  connected() && gotMap && 'up');
  dot('iCam',  fresh('/image_jpeg', 2000) && 'up');
  const nav = fresh('/plan', 2500);              // Nav2 republishes /plan while navigating
  dot('iNav', nav && 'nav');
  $('navTxt').textContent = nav ? '导航中' : '空闲';
  if (!connected()) { $('battTxt').textContent = '--'; $('battFill').setAttribute('width', 0); }
}, 1000);

// ---- battery (2S 18650: 8.4 V full, ~6.9 V empty; buzzer ≈7.6 V) ----
onTopic('/voltage', m => {
  const v = m.data;
  const pct = Math.max(0, Math.min(100, Math.round((v - 6.9) / (8.4 - 6.9) * 100)));
  const col = pct > 45 ? '#7ee2a8' : pct > 25 ? '#f9e2af' : '#f38ba8';
  $('battFill').setAttribute('width', (19 * pct / 100).toFixed(1));
  $('battFill').setAttribute('fill', col);
  $('battTxt').textContent = `${pct}% ${v.toFixed(1)}V`;
  $('battTxt').style.color = col;
});

// ---- SoC temp + CPU/RAM/HD via ssh (needs Tauri backend; hide in browser) ----
function bar(fillId, txtId, pct, txt) {
  $(fillId).setAttribute('width', (25.5 * Math.min(100, pct) / 100).toFixed(1));
  $(fillId).setAttribute('fill', pct > 85 ? '#f38ba8' : pct > 60 ? '#f9e2af' : '#7ea6e0');
  $(txtId).textContent = txt;
}
const gb = mb => (mb / 1024).toFixed(1).replace(/\.0$/, '');
async function pollSys() {
  try {
    // lines: temp(m°C) / loadavg / nproc / "used total" mem MB / "used total" disk MB
    const [tempLine, loadLine, ncLine, memLine, hdLine] =
      (await invoke('sysinfo')).trim().split('\n');
    const t = parseInt(tempLine) / 1000;
    if (t > 0) {
      $('tempTxt').textContent = t.toFixed(0) + '°C';
      $('tempTxt').style.color = t > 75 ? '#f38ba8' : t > 65 ? '#f9e2af' : '#8b93b5';
    }
    const cpu = Math.round(parseFloat(loadLine) / (parseInt(ncLine) || 8) * 100);
    bar('cpuFill', 'cpuTxt', cpu, Math.min(100, cpu) + '%');
    const [mu, mt] = memLine.split(' ').map(Number);
    bar('ramFill', 'ramTxt', mu / mt * 100, `${gb(mu)}/${gb(mt)}G`);
    const [hu, ht] = hdLine.split(' ').map(Number);
    bar('hdFill', 'hdTxt', hu / ht * 100, `${gb(hu)}/${gb(ht)}G`);
  } catch { for (const i of ['tempTxt','cpuTxt','ramTxt','hdTxt']) $(i).textContent = '--'; }
}
if (invoke) { pollSys(); setInterval(pollSys, 5000); }
else for (const g of ['gTemp','gCpu','gRam','gHd']) $(g).style.display = 'none';

// ---- teleop-chain self check (🎮 button -> overlay; fix = restart bringup) ----
const CTL_LABEL = {
  js0: '手柄接收器 /dev/input/js0', joy_node: '手柄读取 joy_node',
  joy_ctrl: '手柄映射 joy_ctrl', driver: '底盘驱动 Mcnamu_driver',
  mux: 'cmd_vel 仲裁 mux', app_conflict: '亚博 APP 抢串口',
  'nav-bringup': '底盘服务 nav-bringup',
};
async function ctlCheck() {
  const out = $('diagout');
  out.textContent = '检查中…';
  try {
    const lines = (await invoke('ctl_check')).trim().split('\n');
    out.innerHTML = lines.map(l => {
      const [key, ...rest] = l.split(' ');
      const val = rest.join(' ');
      const bad = /DEAD|MISSING|YES|inactive|failed/.test(val);
      const cls = bad ? (key === 'app_conflict' ? 'wrn' : 'bad') : 'ok';
      return `<span class="${cls}">${bad ? '✗' : '✓'} ${CTL_LABEL[key] || key}: ${val}</span>`;
    }).join('\n');
  } catch (e) { out.innerHTML = `<span class="bad">检查失败: ${e}</span>`; }
}
$('ctl').onclick = () => { $('diag').style.display = 'flex'; ctlCheck(); };
$('diagre').onclick = ctlCheck;
$('diagclose').onclick = () => $('diag').style.display = 'none';
$('diagfix').onclick = async () => {
  const out = $('diagout');
  out.textContent = '正在重启 nav-bringup（驱动+手柄+mux 一起拉起）…';
  try { await invoke('ctl_fix'); } catch (e) { out.textContent = '重启失败: ' + e; return; }
  out.textContent = '已重启，等节点拉起后自动复检…';
  setTimeout(ctlCheck, 8000);       // early peek: js0/joy_node come up first
  setTimeout(ctlCheck, 20000);      // final: driver/mux need 10-20 s to spawn
};
if (!invoke) $('ctl').style.display = 'none';   // browser mode: no ssh backend

// ---- Safety-brake switch: safety_stop (C++) owns the state (boot = ON).
// GUI and gamepad both just publish a toggle and mirror the latched
// /safety_enabled broadcast — pure rosbridge, works in browser mode too ----
onTopic('/safety_enabled', m => $('mSafety').classList.toggle('on', m.data));
$('mSafety').onclick = () => send({ op:'publish', topic:'/safety_toggle', msg:{} });

// ---- Dog-walk button: dog_walk owns the state and latches /dog_active; the
// GUI mirrors it and, while active, ticks elapsed seconds in the label — that
// count is also the visible proof the click registered, and shows the 3-min
// timeout ticking down its budget. Click again to stop. Refuse-start (safety
// brake off) never latches active, so guard the click and flash a hint (the
// dog_walk node makes the same refusal — three short beeps).
let dogOn = false, dogT0 = 0, dogTimer = null;
function dogLabel() {
  $('mDog').textContent = `🐕 遛狗中 ${Math.floor((performance.now() - dogT0) / 1000)}s`;
}
onTopic('/dog_active', m => {
  dogOn = m.data;
  $('mDog').classList.toggle('on', m.data);
  clearInterval(dogTimer); dogTimer = null;
  if (m.data) { dogT0 = performance.now(); dogLabel(); dogTimer = setInterval(dogLabel, 1000); }
  else $('mDog').textContent = '🐕 遛狗';
});
$('mDog').onclick = () => {
  // Async, no gate: fire the toggle and show transitional text at once; the
  // latched /dog_active reconciles the real state and runs the seconds ticker.
  // No brake/follow gate — dog is lidar-driven itself and just preempts
  // follow's/nav's motion at the mux, never touching their switches.
  send({ op:'publish', topic:'/dog_toggle', msg:{} });
  $('mDog').textContent = dogOn ? '🐕 停止中…' : '🐕 启动中…';
};

// ---- Recording buttons (dashboard + data tab): episode_recorder owns the
// state and latches /recording; every .recbtn just publishes a toggle and
// mirrors the broadcast, so all copies stay in sync for free ----
const recBtns = document.querySelectorAll('.recbtn');
onTopic('/recording', m => recBtns.forEach(b => {
  b.classList.toggle('on', m.data);
  b.textContent = m.data ? '⏺ 录制中' : '⏺ 录制';
}));
recBtns.forEach(b =>
  b.onclick = () => send({ op:'publish', topic:'/record_toggle', msg:{} }));

// ---- Follow-me switch: systemd enable/disable on the board, so the choice
// is remembered and survives both GUI restarts and board reboots ----
async function followRefresh() {
  try {
    const active = (await invoke('follow_get')).trim().split(' ')[0] === 'active';
    $('mFollow').classList.toggle('on', active);
  } catch { /* board unreachable: keep last look */ }
}
async function setFollow(on) {
  $('mFollow').disabled = true;
  $('mFollow').classList.toggle('on', on);       // optimistic; refresh corrects
  if (on) {                                      // starting follow preempts the
    send({ op:'publish', topic:'/dog_stop', msg:{} });   // two lightweight modes
    cancelAllGoals();                            // (single-shot; its own switch
  }                                              // stays untouched — persistence)
  try { await invoke('follow_set', { on }); }
  catch { $('mFollow').classList.toggle('on', !on); }
  finally { $('mFollow').disabled = false; setTimeout(followRefresh, 4000); }
}

if (invoke) {
  followRefresh();
  setInterval(followRefresh, 10000);
  $('mFollow').onclick = () => setFollow(!$('mFollow').classList.contains('on'));
} else $('mFollow').style.display = 'none';

// ---- Mapping mode: mapping.service Conflicts= nav2, so the switch swaps the
// whole stack. Save button only exists while mapping (save = overwrite map,
// switch off without saving = discard and go back to nav) ----
let mappingBusy = false;                           // handover in flight: freeze UI
async function mappingRefresh() {
  if (mappingBusy) return;
  try {
    const on = (await invoke('mapping_get')).trim() === 'active';
    $('mMapping').classList.toggle('on', on);
    $('mMapping').textContent = on ? '🗺 建图中' : '🗺 建图';
    $('mMapSave').style.display = on ? '' : 'none';
  } catch { /* board unreachable: keep last look */ }
}
if (invoke) {
  mappingRefresh();
  setInterval(mappingRefresh, 10000);
  $('mMapping').onclick = async () => {
    if (mappingBusy) return;
    const b = $('mMapping');
    const on = !b.classList.contains('on');
    if (on) $('mMap').click();                     // mapping is watched in map view
    mappingBusy = true;
    b.disabled = true;
    b.textContent = on ? '🗺 启动中…' : '🗺 停止中…';
    $('mMapSave').style.display = 'none';
    try {
      await invoke('mapping_set', { on });         // returns instantly (no-block)
      // poll until the ~20 s nav2<->cartographer handover settles
      for (let i = 0; i < 20; i++) {
        await new Promise(r => setTimeout(r, 2000));
        try { if (((await invoke('mapping_get')).trim() === 'active') === on) break; }
        catch { /* transient ssh miss: keep polling */ }
      }
    } catch (e) { console.error(e); }
    mappingBusy = false;
    b.disabled = false;
    mappingRefresh();
  };
  $('mMapSave').onclick = async () => {
    const b = $('mMapSave');
    b.disabled = true; b.textContent = '💾 保存中…';
    try { await invoke('map_save'); b.textContent = '💾 已保存'; }
    catch (e) { b.textContent = '💾 保存失败'; console.error(e); }
    finally {
      b.disabled = false;
      setTimeout(() => { b.textContent = '💾 存图'; mappingRefresh(); }, 3000);
    }
  };
} else { $('mMapping').style.display = 'none'; $('mMapSave').style.display = 'none'; }
