// Dashboard: TF / map / scan rendering, goal drag, stop button, mode toggle.
import { $, S } from './state.js';
import { onTopic, send, connected, cancelAllGoals, pubTwist } from './ros.js';

const cv = $('cv'), cx = cv.getContext('2d'), stats = $('stats');
let mode = 'map';
let scanMsg = null, mapMsg = null, planMsg = null;
let frames = 0, lastT = performance.now(), fps = 0;
let viewRange = 4.0, zoom = 1.0;
let goalDrag = null;
const tfDict = {};
const mapCanvas = document.createElement('canvas');

onTopic('/scan', m => { scanMsg = m; if (S.page==='dash' && mode==='scan') render(); });
onTopic('/map',  m => { mapMsg = m; rasterizeMap(); if (S.page==='dash' && mode==='map') render(); });
onTopic('/plan', m => { planMsg = m; });
const onTf = m => {
  for (const t of m.transforms) {
    const q = t.transform.rotation;
    tfDict[t.child_frame_id] = { parent: t.header.frame_id,
      x: t.transform.translation.x, y: t.transform.translation.y,
      yaw: Math.atan2(2*(q.w*q.z + q.x*q.y), 1 - 2*(q.y*q.y + q.z*q.z)) };
  }
  if (S.page==='dash' && mode==='map') render();
};
onTopic('/tf', onTf);
onTopic('/tf_static', onTf);

function lookup(target, source) {
  let chain = [], cur = source, guard = 0;
  while (cur !== target) {
    const t = tfDict[cur];
    if (!t || ++guard > 20) return null;
    chain.push(t); cur = t.parent;
  }
  let x = 0, y = 0, yaw = 0;
  for (let i = chain.length - 1; i >= 0; i--) {
    const t = chain[i];
    x += Math.cos(yaw)*t.x - Math.sin(yaw)*t.y;
    y += Math.sin(yaw)*t.x + Math.cos(yaw)*t.y;
    yaw += t.yaw;
  }
  return { x, y, yaw };
}
function robotPose() {
  return lookup('map','base_footprint') || lookup('map','base_link')
      || lookup('odom','base_footprint') || lookup('odom','base_link');
}
function rasterizeMap() {
  const g = mapMsg; if (!g) return;
  const w = g.info.width, h = g.info.height;
  mapCanvas.width = w; mapCanvas.height = h;
  const img = mapCanvas.getContext('2d').createImageData(w, h);
  const d = (typeof g.data === 'string')
    ? Uint8Array.from(atob(g.data), c => c.charCodeAt(0)) : g.data;
  for (let i = 0; i < w*h; i++) {
    let v = d[i]; if (v > 127) v -= 256;
    const row = (i / w) | 0, col = i % w;
    const j = ((h - 1 - row) * w + col) * 4;
    let r, gg, b;
    if (v < 0)       { r=26; gg=30; b=44; }
    else if (v < 50) { r=52; gg=60; b=82; }
    else             { r=249; gg=169; b=74; }
    img.data[j]=r; img.data[j+1]=gg; img.data[j+2]=b; img.data[j+3]=255;
  }
  mapCanvas.getContext('2d').putImageData(img, 0, 0);
}
function w2c() {
  const g = mapMsg.info, W = cv.width, H = cv.height;
  const base = Math.min(W / (g.width*g.resolution), H / (g.height*g.resolution));
  const s = base * zoom;
  const pose = robotPose();
  const cxw = zoom > 1.01 && pose ? pose.x : g.origin.position.x + g.width*g.resolution/2;
  const cyw = zoom > 1.01 && pose ? pose.y : g.origin.position.y + g.height*g.resolution/2;
  return { s,
    px: wx => W/2 + (wx - cxw) * s,
    py: wy => H/2 - (wy - cyw) * s,
    wx: px => (px - W/2)/s + cxw,
    wy: py => -(py - H/2)/s + cyw };
}
export function render() {
  cx.clearRect(0, 0, cv.width, cv.height);
  if (mode === 'map' && mapMsg) renderMap();
  else renderScan();
  frames++;
  const now = performance.now();
  if (now - lastT > 1000) { fps = frames*1000/(now-lastT); frames = 0; lastT = now; }
}
function renderMap() {
  const g = mapMsg.info, t = w2c();
  const ox = t.px(g.origin.position.x);
  const oy = t.py(g.origin.position.y + g.height*g.resolution);
  cx.imageSmoothingEnabled = false;
  cx.drawImage(mapCanvas, ox, oy, g.width*g.resolution*t.s, g.height*g.resolution*t.s);
  if (planMsg && planMsg.poses.length) {
    cx.strokeStyle = '#7ee2a8'; cx.lineWidth = 2; cx.beginPath();
    planMsg.poses.forEach((p, i) => {
      const X = t.px(p.pose.position.x), Y = t.py(p.pose.position.y);
      i ? cx.lineTo(X, Y) : cx.moveTo(X, Y);
    });
    cx.stroke();
  }
  const lp = lookup('map','lidar_link') || lookup('map','laser') || lookup('map','laser_link');
  if (scanMsg && lp) {
    cx.fillStyle = '#f38ba8';
    for (let i = 0; i < scanMsg.ranges.length; i++) {
      const r = scanMsg.ranges[i];
      if (!(r > scanMsg.range_min && r < scanMsg.range_max && isFinite(r))) continue;
      const a = lp.yaw + scanMsg.angle_min + i*scanMsg.angle_increment;
      cx.fillRect(t.px(lp.x + Math.cos(a)*r)-1, t.py(lp.y + Math.sin(a)*r)-1, 2, 2);
    }
  }
  const pose = robotPose();
  if (pose) {
    const X = t.px(pose.x), Y = t.py(pose.y);
    cx.fillStyle = '#7ee2a8';
    cx.beginPath();
    cx.moveTo(X + 12*Math.cos(pose.yaw),    Y - 12*Math.sin(pose.yaw));
    cx.lineTo(X + 8*Math.cos(pose.yaw+2.5), Y - 8*Math.sin(pose.yaw+2.5));
    cx.lineTo(X + 8*Math.cos(pose.yaw-2.5), Y - 8*Math.sin(pose.yaw-2.5));
    cx.closePath(); cx.fill();
  }
  if (goalDrag && goalDrag.x1 !== undefined) {
    cx.strokeStyle = '#f9e2af'; cx.lineWidth = 2; cx.beginPath();
    cx.moveTo(goalDrag.x0, goalDrag.y0); cx.lineTo(goalDrag.x1, goalDrag.y1); cx.stroke();
  }
  stats.textContent = `map ${g.width}×${g.height} @${g.resolution} m · ${fps.toFixed(1)} fps · zoom ${zoom.toFixed(1)}×`
    + (pose ? ` · 位姿 (${pose.x.toFixed(2)}, ${pose.y.toFixed(2)})` : ' · 等TF…');
}
function renderScan() {
  const scan = scanMsg; if (!scan) { stats.textContent = '等 /scan…'; return; }
  const W = cv.width, H = cv.height, R = Math.min(W,H)/2 - 20, cxp = W/2, cyp = H/2;
  const finite = scan.ranges.filter(r => r > scan.range_min && r < scan.range_max && isFinite(r));
  if (finite.length) {
    const s = [...finite].sort((a,b)=>a-b);
    viewRange = Math.max(2, Math.ceil(s[Math.floor(s.length*0.95)]*1.2*2)/2);
  }
  cx.strokeStyle = '#232838'; cx.fillStyle = '#5d6485'; cx.font = '11px system-ui';
  for (let m = 1; m <= viewRange; m++) {
    const r = R * m/viewRange;
    cx.beginPath(); cx.arc(cxp, cyp, r, 0, Math.PI*2); cx.stroke();
    cx.fillText(m + ' m', cxp + r*0.7071 + 3, cyp - r*0.7071 - 3);
  }
  cx.fillStyle = '#7ee2a8';
  cx.beginPath(); cx.moveTo(cxp,cyp-10); cx.lineTo(cxp-6,cyp+8); cx.lineTo(cxp+6,cyp+8);
  cx.closePath(); cx.fill();
  cx.fillStyle = '#f9a94a';
  let n = 0;
  for (let i = 0; i < scan.ranges.length; i++) {
    const r = scan.ranges[i];
    if (!(r > scan.range_min && r < scan.range_max && isFinite(r))) continue;
    const a = scan.angle_min + i*scan.angle_increment;
    cx.fillRect(cxp - Math.sin(a)*r/viewRange*R - 1.5, cyp - Math.cos(a)*r/viewRange*R - 1.5, 3, 3);
    n++;
  }
  stats.textContent = `${n} 点 · ${fps.toFixed(1)} fps · 半径 ${viewRange} m`;
}

// ---- goal drag / wheel zoom / modes / stop ----
function canvasXY(e) {
  const r = cv.getBoundingClientRect();
  return [(e.clientX-r.left)*cv.width/r.width, (e.clientY-r.top)*cv.height/r.height];
}
cv.addEventListener('mousedown', e => {
  if (mode !== 'map' || !mapMsg) return;
  const [x0, y0] = canvasXY(e); goalDrag = { x0, y0 };
});
cv.addEventListener('mousemove', e => {
  if (!goalDrag) return;
  [goalDrag.x1, goalDrag.y1] = canvasXY(e);
  render();
});
cv.addEventListener('mouseup', () => {
  if (goalDrag && !connected()) {
    stats.textContent = '⚠️ 未连接,目标没有发出'; goalDrag = null; render(); return;
  }
  if (!goalDrag || !mapMsg) { goalDrag = null; return; }
  const t = w2c();
  const gx = t.wx(goalDrag.x0), gy = t.wy(goalDrag.y0);
  const yaw = (goalDrag.x1 !== undefined)
    ? Math.atan2(-(goalDrag.y1-goalDrag.y0), goalDrag.x1-goalDrag.x0) : 0;
  goalDrag = null;
  cancelAllGoals();
  planMsg = null;
  setTimeout(() => {
    if (!send({ op:'publish', topic:'/goal_pose', msg:{
      header:{ frame_id:'map', stamp:{sec:0, nanosec:0} },
      pose:{ position:{x:gx, y:gy, z:0},
             orientation:{x:0, y:0, z:Math.sin(yaw/2), w:Math.cos(yaw/2)} } } })) return;
    stats.textContent = `已终止旧目标，新目标 (${gx.toFixed(2)}, ${gy.toFixed(2)}, ${(yaw*180/Math.PI).toFixed(0)}°)`;
  }, 200);
});
cv.addEventListener('wheel', e => {
  if (mode !== 'map') return;
  e.preventDefault();
  zoom = Math.min(10, Math.max(1, zoom * (e.deltaY < 0 ? 1.2 : 1/1.2)));
  render();
}, { passive:false });

function setMode(m) {
  mode = m;
  $('mScan').classList.toggle('on', m==='scan');
  $('mMap').classList.toggle('on', m==='map');
  render();
}
$('mScan').onclick = () => setMode('scan');
$('mMap').onclick  = () => setMode('map');
$('stop').onclick = () => {
  if (!connected()) {
    stats.textContent = '⚠️ 未连接,终止没有发出!等待重连或检查小车'; return;
  }
  cancelAllGoals();
  planMsg = null;
  for (let i = 0; i < 5; i++) setTimeout(() => pubTwist(0,0,0), i*100);
  stats.textContent = '已发送终止：取消导航目标 + 刹停';
};

// Canvas backing store follows its on-screen size; content stays proportional
// because w2c()/renderScan() fit by min(W,H).
new ResizeObserver(() => {
  const r = cv.getBoundingClientRect();
  const w = Math.round(r.width), h = Math.round(r.height);
  if (w > 0 && h > 0 && (cv.width !== w || cv.height !== h)) {
    cv.width = w; cv.height = h; render();
  }
}).observe(cv);
