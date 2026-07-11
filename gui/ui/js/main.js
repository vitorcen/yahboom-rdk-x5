// Assembly: tabs, battery pill, connect wiring. Importing a module registers
// its topic handlers and DOM listeners (side-effect modules by design).
import { $, S } from './state.js';
import { connect, onTopic } from './ros.js';
import './viewer.js';
import './camera.js';
import { keyStop } from './teleop.js';
import { onLogsShown } from './logs.js';

document.querySelectorAll('.tab').forEach(b => b.onclick = () => {
  S.page = b.dataset.page;
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x === b));
  $('page-dash').classList.toggle('on', S.page === 'dash');
  $('page-logs').classList.toggle('on', S.page === 'logs');
  if (S.page === 'logs') { keyStop(); onLogsShown(); }
});

onTopic('/voltage', m => {
  const v = m.data;   // 2S 18650: 8.4 V full, ~6.9 V empty; buzzer ≈7.6 V
  const pct = Math.max(0, Math.min(100, Math.round((v - 6.9) / (8.4 - 6.9) * 100)));
  const el = $('batt');
  el.style.display = 'inline';
  el.textContent = `🔋${pct}% (${v.toFixed(1)}V)` + (pct <= 25 ? ' 请充电' : '');
  el.className = 'pill ' + (pct > 45 ? 'ok' : pct > 25 ? 'warn' : 'bad');
});

$('btn').onclick = connect;
connect();
