// TEST-ONLY stub for test_imu.html (swapped in via import map): feeds the
// imu.js handlers synthetic wobbling data without a rosbridge. Delete freely.
const handlers = {};
export function onTopic(topic, fn) { (handlers[topic] ||= []).push(fn); }
export const connected = () => true;
export function send() {}
export function pubTwist() {}
export function cancelAllGoals() {}
export function connect() {}

const t0 = Date.now();
setInterval(() => {
  const t = (Date.now() - t0) / 1000;
  const roll = 0.35 * Math.sin(t), pitch = 0.25 * Math.cos(0.7 * t), yaw = 0.3 * t;
  const cr = Math.cos(roll / 2), sr = Math.sin(roll / 2);
  const cp = Math.cos(pitch / 2), sp = Math.sin(pitch / 2);
  const cy = Math.cos(yaw / 2), sy = Math.sin(yaw / 2);
  const q = { w: cr * cp * cy + sr * sp * sy, x: sr * cp * cy - cr * sp * sy,
              y: cr * sp * cy + sr * cp * sy, z: cr * cp * sy - sr * sp * cy };
  const gyro = { x: 0.5 * Math.sin(t * 2), y: -0.3 * Math.cos(t), z: 1.2 * Math.sin(t * 0.5) };
  const acc = { x: 1.5 * Math.sin(t), y: 9.8 * Math.sin(roll), z: 9.8 * Math.cos(roll) };
  for (const f of handlers['/imu/data'] || [])
    f({ orientation: q, angular_velocity: gyro, linear_acceleration: acc });
  for (const f of handlers['/camera/imu'] || [])
    f({ orientation: { x: 0, y: 0, z: 0, w: 1 },
        angular_velocity: { x: -gyro.x, y: gyro.z, z: gyro.y },
        linear_acceleration: { x: -0.3, y: -8.7, z: -4.4 } });
  for (const f of handlers['/imu/mag'] || [])
    f({ magnetic_field: { x: 25 * Math.cos(yaw), y: -25 * Math.sin(yaw), z: -10 } });
}, 100);
