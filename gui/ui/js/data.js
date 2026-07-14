// Data tab: episode dataset management + quick review.
// Talks to the ep_* Tauri commands (board-side recording store) and renders
// local previews pulled into <repo>/episodes/<name>/preview/ via convertFileSrc.
import { $, S, invoke } from './state.js';
import { onTopic, send } from './ros.js';

// Tauri v2 asset bridge: turns an absolute FS path into an asset:// URL the
// webview may fetch/<img> (needs assetProtocol scope in tauri.conf.json).
const convertFileSrc = window.__TAURI__ && window.__TAURI__.core
                     ? window.__TAURI__.core.convertFileSrc : null;

// cmd_vel timeline: one vx line per arbitration stage, same colours as the
// system-tab stack legend (joy=blue, mux=purple, drv=orange, final=green).
const CMD_SERIES = [
  { file: 'cmd_vel_joy.csv', color: '#7ea6e0', label: 'joy' },
  { file: 'cmd_vel_mux.csv', color: '#c792ea', label: 'mux' },
  { file: 'cmd_vel_drv.csv', color: '#e0a458', label: 'drv' },
  { file: 'cmd_vel.csv',     color: '#7ee2a8', label: '/cmd_vel' },
];

let episodes = [];
let busy = false;          // an ep_export/pull/delete is in flight: freeze the table
let confirmDel = null;     // name of the row showing inline delete confirm
let view = null;           // active viewer state (see openView)

// ---------------------------------------------------------------- list ----
async function loadList() {
  if (!invoke) return;
  $('dataMeta').textContent = '加载中…';
  let data;
  try { data = JSON.parse(await invoke('ep_list')); }
  catch (e) { $('dataMeta').textContent = '加载失败: ' + e; return; }
  episodes = Array.isArray(data.episodes) ? data.episodes : [];
  // newest first — the name embeds the start timestamp
  episodes.sort((a, b) => b.name.localeCompare(a.name));
  renderDisk(data.disk_free_gb);
  renderTable();
  $('dataMeta').textContent = episodes.length + ' 个 episode';
}

// 4 GB is the recording gate threshold; warn below 8, danger below 4.
function renderDisk(gb) {
  const el = $('dataDisk');
  const v = (gb == null) ? null : Number(gb);
  el.textContent = '板上余量 ' + (v == null ? '--' : v.toFixed(1) + ' GB');
  el.className = 'pill' + (v == null ? '' : v < 4 ? ' bad' : v < 8 ? ' warn' : ' ok');
}

function hasLocal(ep) { return !!ep.preview_local; }
// preview_local is ASSUMED to be the absolute path of the local preview dir.
function localBase(ep) {
  return typeof ep.preview_local === 'string' ? ep.preview_local : null;
}

function badges(ep) {
  let h = '';
  if (ep.partial) h += '<span class="badge bpart">残包</span>';
  if (ep.pulled)  h += '<span class="badge bfull">完整</span>';
  if (hasLocal(ep)) h += '<span class="dotb" title="有本地预览"></span>';
  return h || '<span class="muted">—</span>';
}

function actions(ep) {
  const n = ep.name;
  if (confirmDel === n) {
    return `<span class="delconf">确认删除 ${n}?</span>` +
           `<button class="danger" data-act="delyes" data-name="${n}">删</button>` +
           `<button class="ghost" data-act="delno" data-name="${n}">取消</button>`;
  }
  let h = `<button data-act="view" data-name="${n}">查看</button>`;
  if (!ep.pulled) h += `<button class="mode" data-act="pullfull" data-name="${n}">拉取完整</button>`;
  else h += `<button class="mode" data-act="replay" data-name="${n}" title="本地 RViz 回放(独立 domain,不干扰真车);关掉 RViz 窗口即停">🔭 回放</button>`;
  // partial (残包) rows are un-deletable board-side, so hide the delete button.
  if (!ep.partial) h += `<button class="ghost" data-act="del" data-name="${n}">删除</button>`;
  return h;
}

function renderTable() {
  parkView();               // rescue #epView before innerHTML wipes the tbody
  let html = '';
  for (const ep of episodes) {
    const dur = ep.duration_s != null ? Number(ep.duration_s).toFixed(1) + 's' : '—';
    const mb  = ep.size_mb != null ? Number(ep.size_mb).toFixed(0) : '—';
    html += `<tr><td class="epn">${ep.name}</td><td>${dur}</td><td>${mb}</td>` +
            `<td>${ep.stopped_by || '—'}</td><td>${badges(ep)}</td>` +
            `<td class="epact">${actions(ep)}</td></tr>`;
  }
  $('epBody').innerHTML = html ||
    '<tr><td colspan="6" class="muted">暂无 episode（在仪表盘 ⏺ 录制）</td></tr>';
  if (view) placeView(view.name);   // keep the inline panel across re-renders
}

function setBusy(b) {
  busy = b;
  $('dataRefresh').disabled = b;
  document.querySelectorAll('#epBody button').forEach(x => x.disabled = b);
}

// ------------------------------------------------------------- actions ----
$('epBody').addEventListener('click', async e => {
  const btn = e.target.closest('button');
  if (!btn || busy) return;
  const name = btn.dataset.name, act = btn.dataset.act;
  if (act === 'del')   { confirmDel = name; renderTable(); return; }
  if (act === 'delno') { confirmDel = null; renderTable(); return; }
  if (act === 'delyes') {
    confirmDel = null; setBusy(true);
    try { await invoke('ep_delete', { name }); }
    catch (err) { alert('删除失败: ' + err); }
    setBusy(false); await loadList(); return;
  }
  if (act === 'replay') {
    try { await invoke('ep_replay', { name }); }
    catch (err) { alert('回放启动失败: ' + err); }
    return;
  }
  if (act === 'pullfull') {
    setBusy(true); btn.innerHTML = '<span class="spin"></span>';
    try { await invoke('ep_pull', { name, preview: false }); }
    catch (err) { alert('拉取失败: ' + err); }
    setBusy(false); await loadList(); return;
  }
  if (act === 'view') {
    const ep = episodes.find(x => x.name === name);
    if (ep && !hasLocal(ep)) {
      setBusy(true); btn.innerHTML = '<span class="spin"></span>';
      try {
        await invoke('ep_export', { name, preview: true });
        await invoke('ep_pull', { name, preview: true });
      } catch (err) { alert('准备预览失败: ' + err); setBusy(false); renderTable(); return; }
      setBusy(false); await loadList();
    }
    openView(name);
  }
});

$('dataRefresh').onclick = loadList;

// -------------------------------------------------------------- viewer ----
async function openView(name) {
  const ep = episodes.find(x => x.name === name);
  const base = ep && localBase(ep);
  if (!base) { alert('没有可用的本地预览路径（preview_local）'); return; }
  $('epViewTitle').textContent = name;

  const files = await loadFrames(base);
  const frameTs = files.map(f => {
    const m = f.match(/_(\d+)\.jpg$/);
    return m ? Number(m[1]) / 1e9 : 0;   // ns epoch -> s
  });
  const series = await loadSeries(base);

  // shared time window covers both frames and cmd samples so the cursor always lands
  let lo = Infinity, hi = -Infinity;
  for (const t of frameTs) { if (t) { lo = Math.min(lo, t); hi = Math.max(hi, t); } }
  for (const s of series) for (const p of s.pts) { lo = Math.min(lo, p[0]); hi = Math.max(hi, p[0]); }
  if (!isFinite(lo)) { lo = 0; hi = 1; }
  if (hi <= lo) hi = lo + 1;

  view = { name, base, files, frameTs, series, t0: lo, t1: hi };
  placeView(name);
  await drawOdom(base);
  drawCmd(null);

  const sl = $('epSlider');
  sl.min = 0; sl.max = Math.max(0, files.length - 1); sl.value = 0;
  sl.oninput = () => showFrame(parseInt(sl.value));
  if (files.length) showFrame(0);
  else { $('epFrame').removeAttribute('src'); $('epFrameMeta').textContent = '(无帧,manifest.yaml 缺 files 列表?)'; }
}

function showFrame(i) {
  if (!view || !view.files.length) return;
  const f = view.files[i];
  $('epFrame').src = convertFileSrc(view.base + '/frames/' + f);
  const rel = (view.frameTs[i] - view.t0).toFixed(2);
  $('epFrameMeta').textContent = `帧 ${i + 1}/${view.files.length} · t=${rel}s`;
  drawCmd(view.frameTs[i]);
}

$('epViewClose').onclick = () => { parkView(); view = null; };

// The viewer expands inline under its episode's row (a full-width injected
// <tr>), not in a fixed slot at the bottom of the table.
function parkView() {
  // move #epView back out of the tbody BEFORE any innerHTML rebuild wipes it
  $('epView').style.display = 'none';
  $('datawrap').appendChild($('epView'));
  const row = document.getElementById('epViewRow');
  if (row) row.remove();
}

function placeView(name) {
  parkView();
  const anchor = [...document.querySelectorAll('#epBody tr')]
    .find(r => r.querySelector('.epn')?.textContent === name);
  if (!anchor) return;
  const tr = document.createElement('tr');
  tr.id = 'epViewRow';
  const td = document.createElement('td');
  td.colSpan = 6;
  td.appendChild($('epView'));
  tr.appendChild(td);
  anchor.after(tr);
  $('epView').style.display = 'flex';
}

// Frame list comes from manifest.yaml's `files:` array (ASSUMED to exist) —
// filenames (frame_<idx>_<ns>.jpg) aren't predictable and there's no list command.
async function loadFrames(base) {
  if (!convertFileSrc) return [];
  let txt = '';
  try { txt = await (await fetch(convertFileSrc(base + '/manifest.yaml'))).text(); }
  catch (e) { return []; }
  const files = [];
  for (const line of txt.split('\n')) {
    const m = line.match(/-\s*(\S+\.jpg)/);
    if (m) files.push(m[1].trim());
  }
  return files.sort();
}

async function loadSeries(base) {
  const out = [];
  for (const c of CMD_SERIES) {
    let txt = '';
    try { txt = await (await fetch(convertFileSrc(base + '/' + c.file))).text(); }
    catch (e) { continue; }
    const rows = parseCsv(txt);              // header t,vx,vy,wz
    const pts = rows.filter(r => r.length >= 2).map(r => [r[0], r[1]]);
    if (pts.length) out.push({ color: c.color, label: c.label, pts });
  }
  return out;
}

function parseCsv(txt) {
  const lines = txt.trim().split('\n');
  const rows = [];
  for (let i = 1; i < lines.length; i++) {          // skip header
    if (!lines[i]) continue;
    const p = lines[i].split(',').map(Number);
    if (p.every(Number.isFinite)) rows.push(p);
  }
  return rows;
}

// odom path: equal-aspect x/y trace, start green, end red.
async function drawOdom(base) {
  const cv = $('epOdom'), ctx = cv.getContext('2d');
  ctx.clearRect(0, 0, cv.width, cv.height);
  let txt = '';
  try { txt = await (await fetch(convertFileSrc(base + '/odom.csv'))).text(); }
  catch (e) {}
  const rows = parseCsv(txt);                        // t,x,y,yaw,...
  const pts = rows.map(r => [r[1], r[2]]).filter(p => Number.isFinite(p[0]));
  if (pts.length < 2) { odomEmpty(ctx, cv); return; }
  let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
  for (const [x, y] of pts) { xmin = Math.min(xmin, x); xmax = Math.max(xmax, x); ymin = Math.min(ymin, y); ymax = Math.max(ymax, y); }
  const pad = 16, W = cv.width - 2 * pad, H = cv.height - 2 * pad;
  const span = Math.max(xmax - xmin, ymax - ymin, 0.1);   // equal aspect
  const cx = (xmin + xmax) / 2, cy = (ymin + ymax) / 2;
  const sc = Math.min(W, H) / span;
  // ROS x forward (up on screen), y left: map x->screen up, y->screen left
  const px = p => pad + W / 2 - (p[1] - cy) * sc;
  const py = p => pad + H / 2 - (p[0] - cx) * sc;
  ctx.strokeStyle = '#7ea6e0'; ctx.lineWidth = 1.5; ctx.beginPath();
  pts.forEach((p, i) => i ? ctx.lineTo(px(p), py(p)) : ctx.moveTo(px(p), py(p)));
  ctx.stroke();
  dot(ctx, px(pts[0]), py(pts[0]), '#7ee2a8');
  dot(ctx, px(pts[pts.length - 1]), py(pts[pts.length - 1]), '#f38ba8');
}
function odomEmpty(ctx, cv) {
  ctx.fillStyle = '#5d6485'; ctx.font = '11px system-ui';
  ctx.fillText('无 odom 轨迹', 12, cv.height / 2);
}
function dot(ctx, x, y, col) {
  ctx.fillStyle = col; ctx.beginPath(); ctx.arc(x, y, 4, 0, 7); ctx.fill();
}

// cmd_vel timeline: vx lines vs time, optional vertical cursor at cursorT (s).
function drawCmd(cursorT) {
  const cv = $('epCmd'), ctx = cv.getContext('2d');
  ctx.clearRect(0, 0, cv.width, cv.height);
  if (!view) return;
  const pad = 22, W = cv.width - 2 * pad, H = cv.height - 2 * pad;
  const { t0, t1 } = view;
  let vmin = 0, vmax = 0;
  for (const s of view.series) for (const p of s.pts) { vmin = Math.min(vmin, p[1]); vmax = Math.max(vmax, p[1]); }
  if (vmax - vmin < 0.2) { vmax += 0.1; vmin -= 0.1; }
  const X = t => pad + (t - t0) / (t1 - t0) * W;
  const Y = v => pad + (vmax - v) / (vmax - vmin) * H;
  // zero baseline
  ctx.strokeStyle = '#262b3d'; ctx.lineWidth = 1; ctx.beginPath();
  ctx.moveTo(pad, Y(0)); ctx.lineTo(pad + W, Y(0)); ctx.stroke();
  ctx.fillStyle = '#5d6485'; ctx.font = '9px ui-monospace,monospace';
  ctx.fillText('vx', 2, pad + 8); ctx.fillText('t/s', pad + W - 14, cv.height - 4);
  let lx = pad;
  for (const s of view.series) {
    ctx.strokeStyle = s.color; ctx.lineWidth = 1.3; ctx.beginPath();
    s.pts.forEach((p, i) => i ? ctx.lineTo(X(p[0]), Y(p[1])) : ctx.moveTo(X(p[0]), Y(p[1])));
    ctx.stroke();
    ctx.fillStyle = s.color; ctx.fillText(s.label, lx, 10); lx += ctx.measureText(s.label).width + 12;
  }
  if (cursorT != null) {
    ctx.strokeStyle = '#f9e2af'; ctx.lineWidth = 1; ctx.beginPath();
    ctx.moveTo(X(cursorT), pad - 4); ctx.lineTo(X(cursorT), pad + H); ctx.stroke();
  }
}

// called by main.js when the data tab becomes visible
export function onDataShown() { loadList(); }

// A finished recording (latched /recording flips true -> false) means a new
// episode just landed on the board — refresh so it shows up without a click.
let wasRecording = false;
onTopic('/recording', m => {
  if (wasRecording && !m.data && invoke) loadList();
  wasRecording = m.data;
});

// Per-episode duration cap: plain ROS parameter on the recorder node, set
// over rosbridge's generic service call. Takes effect from the next start.
$('epMaxMin').onchange = () => {
  const min = parseFloat($('epMaxMin').value);
  if (!(min > 0)) return;
  send({ op: 'call_service',
    service: '/episode_recorder/set_parameters',
    type: 'rcl_interfaces/srv/SetParameters',
    args: { parameters: [{ name: 'max_duration_s',
                           value: { type: 3, double_value: min * 60 } }] } });
};
