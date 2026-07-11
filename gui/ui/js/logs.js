// Logs tab: live /rosout (rosbridge) + journald/dmesg over ssh (Tauri command).
import { $, S, invoke } from './state.js';
import { onTopic } from './ros.js';

let curSrc = 'rosout';
const rosoutBuf = [];              // ring buffer of parsed /rosout entries
const ROSOUT_MAX = 3000;
let journalText = '';              // raw text of current journald/dmesg fetch
const lvlOn = new Set([20, 30, 40, 50]);   // FATAL rides with ERROR button
const logview = $('logview');

onTopic('/rosout', m => {
  // rcl_interfaces/Log: level 10/20/30/40/50, name, msg, stamp{sec,nanosec}
  const d = new Date(m.stamp.sec * 1000);
  rosoutBuf.push({ lv: m.level, name: m.name, msg: m.msg,
                   tm: d.toTimeString().slice(0, 8) });
  if (rosoutBuf.length > ROSOUT_MAX) rosoutBuf.splice(0, rosoutBuf.length - ROSOUT_MAX);
  if (S.page === 'logs' && curSrc === 'rosout') renderLogs();
});

function esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function renderLogs() {
  const q = $('logfilter').value.toLowerCase();
  let html = '', shown = 0, total = 0;
  if (curSrc === 'rosout') {
    total = rosoutBuf.length;
    for (const e of rosoutBuf) {
      if (!lvlOn.has(e.lv >= 50 ? 40 : e.lv)) continue;   // FATAL shares the ERROR toggle
      if (q && !(e.name + ' ' + e.msg).toLowerCase().includes(q)) continue;
      html += `<span class="ln l${e.lv}"><span class="tm">${e.tm}</span> <span class="nd" data-nd="${esc(e.name)}">[${esc(e.name)}]</span> ${esc(e.msg)}</span>`;
      shown++;
    }
  } else {
    const lines = journalText.split('\n');
    total = lines.length;
    for (const l of lines) {
      if (!l) continue;
      if (q && !l.toLowerCase().includes(q)) continue;
      const low = l.toLowerCase();
      const cls = /error|fail|fatal|segfault|abort/.test(low) ? 'jerr'
                : /warn/.test(low) ? 'jwarn' : '';
      html += `<span class="ln ${cls}">${esc(l)}</span>`;
      shown++;
    }
  }
  logview.innerHTML = html;
  $('logpath').textContent = '📁 ' + pathLine(curSrc);
  $('logmeta').textContent = `${shown}/${total} 行 · 源:${curSrc}`;
  if ($('ascroll').checked) logview.scrollTop = logview.scrollHeight;
}
// click a node name -> filter by it
logview.addEventListener('click', e => {
  const nd = e.target.dataset && e.target.dataset.nd;
  if (nd) { $('logfilter').value = nd; renderLogs(); }
});

// Where each log physically lives on the board (192.168.3.187).
const LOGPATH = {
  rosout: '话题 /rosout (rosbridge ws:9090) · 节点落盘: 板端 /tmp/roslog/<launch>/launch.log (ROS_LOG_DIR, 重启清空)',
  dmesg:  '内核环形缓冲 (ssh dmesg) · 落盘: 板端 /var/log/kern.log',
};
function pathLine(src) {
  if (LOGPATH[src]) return LOGPATH[src];
  return `ssh journalctl -u ${src} · 落盘: 板端 /var/log/journal/9a475307317843438438286387b52448/ (persistent)`;
}
let fetchSeq = 0;                  // drop stale responses when switching sources fast
async function fetchJournal() {
  if (curSrc === 'rosout') return;
  if (!invoke) { journalText = '(journald 日志需要在 Tauri 应用内查看，浏览器里只有 /rosout)'; renderLogs(); return; }
  const seq = ++fetchSeq, src = curSrc;
  $('logmeta').textContent = `拉取 ${src} 中…`;
  let text;
  try {
    text = await invoke('journal', { source: src, lines: parseInt($('jlines').value) });
  } catch (err) {
    text = '拉取失败: ' + err;
  }
  if (seq !== fetchSeq || src !== curSrc) return;   // a newer fetch or source switch won
  journalText = text;
  renderLogs();
}
async function refreshStatus() {
  if (!invoke) return;
  try {
    const out = await invoke('service_status');
    for (const line of out.trim().split('\n')) {
      const [name, state] = line.split(' ');
      const dot = $('dot-' + name);
      if (dot) dot.className = 'dot ' + (state === 'active' ? 'up' : 'down');
    }
  } catch (e) { /* board unreachable: leave dots gray */ }
  try { $('boardinfo').textContent = await invoke('board_info'); } catch (e) {}
}

// called by main.js when the logs tab becomes visible
export function onLogsShown() {
  refreshStatus();
  if (curSrc !== 'rosout') fetchJournal();
}

document.querySelectorAll('.src').forEach(el => el.onclick = () => {
  document.querySelectorAll('.src').forEach(x => x.classList.toggle('on', x === el));
  curSrc = el.dataset.src;
  if (curSrc === 'rosout') renderLogs(); else fetchJournal();
});
document.querySelectorAll('.lvl').forEach(b => b.onclick = () => {
  const lv = parseInt(b.dataset.lv);
  b.classList.toggle('on');
  b.classList.contains('on') ? lvlOn.add(lv) : lvlOn.delete(lv);
  renderLogs();
});
$('logfilter').oninput = renderLogs;
$('jrefresh').onclick = fetchJournal;
$('lclear').onclick = () => { rosoutBuf.length = 0; journalText = ''; renderLogs(); };
setInterval(() => {
  if (S.page !== 'logs') return;
  if ($('jauto').checked && curSrc !== 'rosout') fetchJournal();
}, 5000);
setInterval(() => { if (S.page === 'logs') refreshStatus(); }, 10000);
