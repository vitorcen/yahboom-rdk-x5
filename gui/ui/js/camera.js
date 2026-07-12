// Camera window: drag anywhere to move; corner grip resizes aspect-locked
// (hand-rolled — WebKitGTK's native CSS resize handle is unreliable).
import { $ } from './state.js';
import { onTopic } from './ros.js';

const cambox = $('cambox');
let camDrag = null, camResize = null;

onTopic('/image_jpeg', m => {
  $('cam').src = 'data:image/jpeg;base64,' + m.data;
  cambox.style.display = 'block';
});

// Perception overlay: body/head/face/hand boxes + gesture labels from the
// follow-me BPU chain, drawn over the (object-fit: cover) camera image.
// Source frame is 960x544; silence >1s clears the overlay.
const SRC_W = 960, SRC_H = 544;
const ROI_COLOR = { body:'#7ea6e0', head:'#8b93b5', face:'#7ee2a8', hand:'#e0a458' };
const GESTURE = { 2:'👍', 3:'✌V', 4:'🤫嘘', 5:'✋Palm 停止', 11:'👌OK 锁定',
                  12:'👉', 13:'👈', 14:'🤟666' };
let ovTimer = null;

onTopic('/hobot_hand_gesture_detection', m => {
  const cv = $('camov');
  if (cambox.style.display === 'none') return;
  cv.width = cambox.clientWidth; cv.height = cambox.clientHeight;
  const g = cv.getContext('2d');
  g.clearRect(0, 0, cv.width, cv.height);
  // replicate object-fit: cover mapping
  const s = Math.max(cv.width / SRC_W, cv.height / SRC_H);
  const ox = (cv.width - SRC_W * s) / 2, oy = (cv.height - SRC_H * s) / 2;
  g.font = '12px system-ui'; g.lineWidth = 2;
  for (const t of m.targets || []) {
    const gests = (t.attributes || []).filter(a => a.type.includes('gesture'))
                                      .map(a => Math.round(a.value));
    for (const r of t.rois || []) {
      const c = ROI_COLOR[r.type] || '#c792ea';
      const x = ox + r.rect.x_offset * s, y = oy + r.rect.y_offset * s;
      const w = r.rect.width * s, h = r.rect.height * s;
      g.strokeStyle = c; g.strokeRect(x, y, w, h);
      g.fillStyle = c;
      let lbl = r.type === 'body' ? `${r.type}#${t.track_id}` : r.type;
      if (r.type === 'hand' && gests.length)
        lbl += ' ' + gests.map(v => GESTURE[v] || v).join(' ');
      g.fillText(lbl, x + 2, y > 14 ? y - 3 : y + 12);
    }
    // gesture on a roi-less target (defensive: pipeline variants differ)
    if (!(t.rois || []).length && gests.length) {
      g.fillStyle = '#e0a458';
      g.fillText(`#${t.track_id} ` + gests.map(v => GESTURE[v] || v).join(' '), 8, 16);
    }
  }
  clearTimeout(ovTimer);
  ovTimer = setTimeout(() => {
    const c2 = $('camov');
    c2.getContext('2d').clearRect(0, 0, c2.width, c2.height);
  }, 1000);
});

$('camgrip').addEventListener('mousedown', e => {
  camResize = { w0: cambox.offsetWidth, h0: cambox.offsetHeight,
                x0: e.clientX, y0: e.clientY };
  e.stopPropagation(); e.preventDefault();
});
cambox.addEventListener('mousedown', e => {
  camDrag = { dx: e.clientX - cambox.offsetLeft, dy: e.clientY - cambox.offsetTop };
  e.preventDefault();
});
window.addEventListener('mousemove', e => {
  if (camResize) {
    // aspect-locked: follow whichever axis moved more, height = width / ratio
    const ratio = camResize.w0 / camResize.h0;
    const dx = e.clientX - camResize.x0, dy = e.clientY - camResize.y0;
    const d = Math.abs(dx) >= Math.abs(dy) ? dx : dy * ratio;
    const w = Math.max(120, camResize.w0 + d);
    cambox.style.width  = w + 'px';
    cambox.style.height = (w / ratio) + 'px';
    return;
  }
  if (!camDrag) return;
  const wrap = $('wrap');
  const x = Math.min(Math.max(e.clientX - camDrag.dx, 0), wrap.clientWidth - cambox.offsetWidth);
  const y = Math.min(Math.max(e.clientY - camDrag.dy, 0), wrap.clientHeight - cambox.offsetHeight);
  cambox.style.left = x + 'px'; cambox.style.top = y + 'px'; cambox.style.right = 'auto';
});
window.addEventListener('mouseup', () => { camDrag = null; camResize = null; });
