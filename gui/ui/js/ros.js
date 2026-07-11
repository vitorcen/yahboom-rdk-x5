// rosbridge v2 connection. Modules claim topics via onTopic(); nobody touches
// the raw WebSocket — publish through send()/pubTwist().
import { $ } from './state.js';

let ws = null, reconnectTimer = null;
const handlers = {};                       // topic -> [fn(msg)]

export function onTopic(topic, fn) { (handlers[topic] ||= []).push(fn); }
export const connected = () => !!ws && ws.readyState === 1;
export function send(obj) {
  if (!connected()) return false;
  ws.send(JSON.stringify(obj));
  return true;
}
export function pubTwist(vx, vy, wz) {
  return send({ op:'publish', topic:'/cmd_vel',
    msg:{ linear:{x:vx,y:vy,z:0}, angular:{x:0,y:0,z:wz} } });
}
export function cancelAllGoals() {
  send({ op:'call_service',
    service:'/navigate_to_pose/_action/cancel_goal', type:'action_msgs/srv/CancelGoal',
    args:{ goal_info:{ goal_id:{ uuid:[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0] },
                       stamp:{ sec:0, nanosec:0 } } } });
}

export function connect() {
  clearTimeout(reconnectTimer);
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  ws = new WebSocket($('url').value);
  const st = $('st');
  st.textContent = '连接中…'; st.className = 'pill';
  ws.onopen = () => {
    st.textContent = '已连接'; st.className = 'pill ok';
    $('dot-rosout').className = 'dot up';
    const sub = (topic, type, throttle) => ws.send(JSON.stringify(
      { op:'subscribe', topic, type, throttle_rate: throttle||0, queue_length:1 }));
    sub('/scan', 'sensor_msgs/LaserScan');
    sub('/map', 'nav_msgs/OccupancyGrid', 1000);
    sub('/plan', 'nav_msgs/Path', 500);
    sub('/tf', 'tf2_msgs/TFMessage', 100);
    sub('/tf_static', 'tf2_msgs/TFMessage');
    sub('/image_jpeg', 'sensor_msgs/CompressedImage', 66);
    sub('/voltage', 'std_msgs/Float32', 5000);
    sub('/rosout', 'rcl_interfaces/msg/Log');
    ws.send(JSON.stringify({ op:'advertise', topic:'/goal_pose', type:'geometry_msgs/PoseStamped' }));
    ws.send(JSON.stringify({ op:'advertise', topic:'/cmd_vel', type:'geometry_msgs/Twist' }));
  };
  ws.onclose = () => { st.textContent = '已断开,3s后重连…'; st.className = 'pill bad';
                       $('dot-rosout').className = 'dot down';
                       clearTimeout(reconnectTimer); reconnectTimer = setTimeout(connect, 3000); };
  ws.onerror  = () => { st.textContent = '连接失败'; st.className = 'pill bad'; };
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.op !== 'publish') return;
    for (const f of handlers[m.topic] || []) f(m.msg);
  };
}
