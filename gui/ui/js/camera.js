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
