// Shared floating-window behavior: drag anywhere to move (clamped to the
// positioned parent, i.e. #wrap); corner grip resizes aspect-locked (follow
// whichever axis moved more). Hand-rolled — WebKitGTK's native CSS resize
// handle is unreliable. Used by both the camera (right) and depth (left) windows.
export function makeFloatBox(box, grip) {
  let drag = null, resize = null;
  grip.addEventListener('mousedown', e => {
    resize = { w0: box.offsetWidth, h0: box.offsetHeight, x0: e.clientX, y0: e.clientY };
    e.stopPropagation(); e.preventDefault();
  });
  box.addEventListener('mousedown', e => {
    drag = { dx: e.clientX - box.offsetLeft, dy: e.clientY - box.offsetTop };
    e.preventDefault();
  });
  window.addEventListener('mousemove', e => {
    if (resize) {
      const ratio = resize.w0 / resize.h0;
      const dx = e.clientX - resize.x0, dy = e.clientY - resize.y0;
      const d = Math.abs(dx) >= Math.abs(dy) ? dx : dy * ratio;
      const wrap = box.offsetParent;
      // clamp between a floor and the parent size, so the box can't grow past
      // #wrap (which would push the drag bounds negative and lose the window)
      const maxW = Math.max(120, wrap.clientWidth - box.offsetLeft);
      const w = Math.min(maxW, Math.max(120, resize.w0 + d));
      box.style.width  = w + 'px';
      box.style.height = (w / ratio) + 'px';
      return;
    }
    if (!drag) return;
    const wrap = box.offsetParent;                 // #wrap (position:relative)
    const x = Math.min(Math.max(e.clientX - drag.dx, 0), wrap.clientWidth  - box.offsetWidth);
    const y = Math.min(Math.max(e.clientY - drag.dy, 0), wrap.clientHeight - box.offsetHeight);
    box.style.left = x + 'px'; box.style.top = y + 'px'; box.style.right = 'auto';
  });
  window.addEventListener('mouseup', () => { drag = null; resize = null; });
}
