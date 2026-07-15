// Camera window: drag anywhere to move; corner grip resizes aspect-locked
// (hand-rolled — WebKitGTK's native CSS resize handle is unreliable).
import { $ } from './state.js';
import { onTopic } from './ros.js';
import { makeFloatBox } from './floatbox.js';

const cambox = $('cambox');

// Show only while frames arrive; hide on stream loss so a camera-mode switch
// (stereo<->mono) doesn't leave a stale frame frozen on screen.
let camTimer = null;
onTopic('/image_jpeg', m => {
  $('cam').src = 'data:image/jpeg;base64,' + m.data;
  cambox.style.display = 'block';
  clearTimeout(camTimer);
  camTimer = setTimeout(() => { cambox.style.display = 'none'; }, 3000);
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
                                      .map(a => Math.round(a.value))
                                      .filter(v => v);   // 0 = classifier idle
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

makeFloatBox(cambox, $('camgrip'));

// ---- Depth window (left): Orbbec Astra depth colorized to JPEG on the board
// (astra_preview.py). Same "show only while a stream arrives" behavior as the
// camera window — so when the depth chain is off the window stays hidden.
// Red = near, blue = far.
const depthbox = $('depthbox');
let depthTimer = null;
onTopic('/camera/depth/color_jpeg', m => {
  $('depth').src = 'data:image/jpeg;base64,' + m.data;
  depthbox.style.display = 'block';
  clearTimeout(depthTimer);
  depthTimer = setTimeout(() => { depthbox.style.display = 'none'; }, 2000);
});
makeFloatBox(depthbox, $('depthgrip'));
