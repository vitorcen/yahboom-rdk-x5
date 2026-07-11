// Keyboard teleop (dash tab only). Publishes /cmd_vel — the board mux keeps
// the physical joystick (/cmd_vel_joy) at higher priority, and its idle
// watchdog resumes the zero stream once we stop.
import { $, S } from './state.js';
import { pubTwist } from './ros.js';

const KEY_LIN = 0.15, KEY_ANG = 0.8;              // m/s, rad/s
const KEYMAP = {                                  // key -> [vx, vy, wz] sign
  ArrowUp:[1,0,0], w:[1,0,0], ArrowDown:[-1,0,0], s:[-1,0,0],
  ArrowLeft:[0,0,1], a:[0,0,1], ArrowRight:[0,0,-1], d:[0,0,-1],
  q:[0,1,0], e:[0,-1,0],                          // mecanum strafe
};
const keysDown = new Set();
let keyTimer = null;

function keyTick() {
  let vx = 0, vy = 0, wz = 0;
  for (const k of keysDown) { const m = KEYMAP[k]; vx += m[0]; vy += m[1]; wz += m[2]; }
  pubTwist(Math.sign(vx)*KEY_LIN, Math.sign(vy)*KEY_LIN, Math.sign(wz)*KEY_ANG);
  $('iKey').firstElementChild.className = 'dot up';   // light the ⌨️ indicator
  $('keyTxt').textContent = [...keysDown].map(k => k.replace('Arrow','')).join('+');
}
export function keyStop() {
  keysDown.clear();
  if (keyTimer) { clearInterval(keyTimer); keyTimer = null; pubTwist(0,0,0); }
  $('iKey').firstElementChild.className = 'dot';
  $('keyTxt').textContent = '';
}
window.addEventListener('keydown', e => {
  if (S.page !== 'dash' || e.repeat) return;
  const t = e.target.tagName;
  if (t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT') return;
  if (e.key === ' ') { e.preventDefault(); keyStop(); $('stop').click(); return; }
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  if (!KEYMAP[k]) return;
  e.preventDefault();
  keysDown.add(k);
  if (!keyTimer) { keyTick(); keyTimer = setInterval(keyTick, 100); }  // 10 Hz stream
});
window.addEventListener('keyup', e => {
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  if (!keysDown.delete(k)) return;
  keysDown.size ? keyTick() : keyStop();
});
window.addEventListener('blur', keyStop);   // lost focus = dead-man release
