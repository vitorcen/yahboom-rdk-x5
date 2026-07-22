// IMU instrument panel (#imubox, floating over the dashboard canvas):
// attitude ball + mag compass from the chassis 9-axis (madgwick /imu/data +
// /imu/mag), and live gyro/accel bars for both the chassis IMU and the
// GS130WI camera's on-board ICM-42688-P (/camera/imu). Sources go grey when
// their stream stalls; the box appears on the first IMU message.
import { $ } from './state.js';
import { onTopic } from './ros.js';
import { makeFloatBox } from './floatbox.js';

const box = $('imubox'), cv = $('imucv'), ctx = cv.getContext('2d');
makeFloatBox(box, $('imugrip'));

const FRESH = 1.5;                                // s, else source drawn grey
const src = {
  chassis: { t: 0, gyro: [0, 0, 0], acc: [0, 0, 0], rpy: [0, 0, 0] },
  cam:     { t: 0, gyro: [0, 0, 0], acc: [0, 0, 0] },
  mag:     { t: 0, hdg: 0 },
};

function quatToRpy(q) {
  const { x, y, z, w } = q;
  return [
    Math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)),
    Math.asin(Math.max(-1, Math.min(1, 2 * (w * y - z * x)))),
    Math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)),
  ];
}

function grab(s, m) {
  s.t = Date.now() / 1000;
  s.gyro = [m.angular_velocity.x, m.angular_velocity.y, m.angular_velocity.z];
  s.acc = [m.linear_acceleration.x, m.linear_acceleration.y, m.linear_acceleration.z];
  box.style.display = 'block';
}
onTopic('/imu/data', m => { grab(src.chassis, m); src.chassis.rpy = quatToRpy(m.orientation); });
onTopic('/camera/imu', m => grab(src.cam, m));
onTopic('/imu/mag', m => {
  src.mag.t = Date.now() / 1000;
  // raw horizontal heading, no tilt compensation (the car is mostly level)
  src.mag.hdg = Math.atan2(-m.magnetic_field.y, m.magnetic_field.x);
  box.style.display = 'block';
});

const deg = r => (r * 180 / Math.PI);
const fresh = s => Date.now() / 1000 - s.t < FRESH;

function horizon(x0, y0, r, roll, pitch, ok) {
  ctx.save();
  ctx.beginPath(); ctx.arc(x0, y0, r, 0, 7); ctx.clip();
  if (!ok) { ctx.fillStyle = '#1a1e2c'; ctx.fillRect(x0 - r, y0 - r, 2 * r, 2 * r); }
  else {
    ctx.translate(x0, y0); ctx.rotate(-roll);
    const off = Math.max(-r, Math.min(r, pitch / (Math.PI / 4) * r));  // ±45° full scale
    ctx.fillStyle = '#2d5f8f'; ctx.fillRect(-r, -r + off, 2 * r, 2 * r);   // sky
    ctx.fillStyle = '#6b4a2b'; ctx.fillRect(-r, off, 2 * r, 2 * r);        // ground
    ctx.strokeStyle = '#e8edf7'; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(-r, off); ctx.lineTo(r, off); ctx.stroke();
    ctx.lineWidth = 1; ctx.strokeStyle = 'rgba(232,237,247,.6)'; ctx.fillStyle = 'rgba(232,237,247,.6)';
    ctx.font = '10px sans-serif'; ctx.textAlign = 'center';
    for (const p of [-30, -15, 15, 30]) {
      const yy = off - p / 45 * r, w = p % 30 ? 14 : 24;
      ctx.beginPath(); ctx.moveTo(-w, yy); ctx.lineTo(w, yy); ctx.stroke();
      ctx.fillText(Math.abs(p), w + 12, yy + 3);
    }
    ctx.restore(); ctx.save();
  }
  // fixed aircraft symbol + rim
  ctx.strokeStyle = '#f9e2af'; ctx.lineWidth = 3;
  ctx.beginPath(); ctx.moveTo(x0 - r * .55, y0); ctx.lineTo(x0 - r * .18, y0);
  ctx.moveTo(x0 + r * .18, y0); ctx.lineTo(x0 + r * .55, y0);
  ctx.moveTo(x0, y0 - 4); ctx.lineTo(x0, y0 + 4); ctx.stroke();
  ctx.restore();
  ctx.strokeStyle = '#31344b'; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.arc(x0, y0, r, 0, 7); ctx.stroke();
}

function compass(x0, y0, r, hdg, ok) {
  ctx.strokeStyle = '#31344b'; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.arc(x0, y0, r, 0, 7); ctx.stroke();
  ctx.fillStyle = ok ? '#8b93b5' : '#3a3f55'; ctx.font = '10px sans-serif'; ctx.textAlign = 'center';
  ctx.fillText('N', x0, y0 - r + 10);
  if (!ok) return;
  ctx.save(); ctx.translate(x0, y0); ctx.rotate(hdg);
  ctx.strokeStyle = '#f38ba8'; ctx.lineWidth = 2.5;
  ctx.beginPath(); ctx.moveTo(0, r * .55); ctx.lineTo(0, -r * .75); ctx.stroke();
  ctx.fillStyle = '#f38ba8';
  ctx.beginPath(); ctx.moveTo(0, -r * .75); ctx.lineTo(-4, -r * .4); ctx.lineTo(4, -r * .4); ctx.fill();
  ctx.restore();
}

// centered-zero bar triplet: gyro ±2 rad/s, accel ±12 m/s² (post-gravity clip)
function bars(x0, y0, w, vals, span, color, ok) {
  const bh = 12, gap = 17;
  vals.forEach((v, i) => {
    const y = y0 + i * gap;
    ctx.fillStyle = '#1a1e2c'; ctx.fillRect(x0, y, w, bh);
    if (ok) {
      const half = w / 2, len = Math.max(-half, Math.min(half, v / span * half));
      ctx.fillStyle = color;
      ctx.fillRect(x0 + half + Math.min(0, len), y + 2, Math.abs(len), bh - 4);
    }
    ctx.strokeStyle = '#31344b';
    ctx.beginPath(); ctx.moveTo(x0 + w / 2, y); ctx.lineTo(x0 + w / 2, y + bh); ctx.stroke();
    ctx.fillStyle = ok ? '#8b93b5' : '#3a3f55'; ctx.font = '10px sans-serif'; ctx.textAlign = 'left';
    ctx.fillText('xyz'[i], x0 - 10, y + bh - 2);
  });
}

function block(x0, title, s, ok) {
  ctx.fillStyle = ok ? '#cdd6f4' : '#3a3f55'; ctx.font = 'bold 13px sans-serif'; ctx.textAlign = 'left';
  ctx.fillText(title, x0, 30);
  ctx.fillStyle = ok ? '#7ee2a8' : '#3a3f55'; ctx.font = '10px sans-serif';
  ctx.fillText(ok ? '●' : '○', x0 + 64, 30);
  ctx.fillStyle = '#8b93b5'; ctx.font = '11px sans-serif';
  ctx.fillText('陀螺 rad/s', x0, 52);
  bars(x0 + 14, 60, 100, s.gyro, 2, '#7ea6e0', ok);
  ctx.fillStyle = '#8b93b5'; ctx.fillText('加速度 m/s²', x0, 128);
  bars(x0 + 14, 136, 100, s.acc, 12, '#e5c07b', ok);
  if (ok) {
    const a = Math.hypot(...s.acc);
    ctx.fillStyle = '#8b93b5'; ctx.font = '10px sans-serif';
    ctx.fillText(`|a| ${a.toFixed(1)}`, x0 + 68, 128);
  }
}

function draw() {
  if (box.style.display === 'none' || !box.offsetParent) return;
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.save(); ctx.scale(2, 2);                    // 680x400 canvas, 340x200 UI px
  const cOk = fresh(src.chassis), mOk = fresh(src.mag), iOk = fresh(src.cam);
  const [roll, pitch, yaw] = src.chassis.rpy;
  horizon(52, 62, 44, roll, pitch, cOk);
  ctx.fillStyle = cOk ? '#cdd6f4' : '#3a3f55'; ctx.font = '10px sans-serif'; ctx.textAlign = 'center';
  ctx.fillText(cOk ? `R ${deg(roll).toFixed(0)}°  P ${deg(pitch).toFixed(0)}°` : '姿态 --', 52, 121);
  ctx.fillText(cOk ? `Y ${deg(yaw).toFixed(0)}°` : '', 52, 133);
  compass(52, 168, 24, src.mag.hdg, mOk);
  block(115, '底盘 9轴', src.chassis, cOk);
  block(232, '相机 6轴', src.cam, iOk);
  ctx.restore();
}
setInterval(draw, 100);
