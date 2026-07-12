#!/usr/bin/env python3
"""Follow-me: continuous camera + lidar fusion, one 10 Hz control loop.

Two sensor channels run in parallel the whole time (no mode switching):
  camera (30 Hz)  identity: track id, gestures, body bearing, size estimate
  lidar  (10 Hz)  geometry: the leg cluster, gated around the camera bearing
                  while the camera sees the body, around its own prediction
                  when the camera drops out (close range, sharp turns)
Control runs in the lidar callback: bearing from the camera when fresh (it
carries identity), else from the leg track; distance from the leg track when
fresh (far more accurate than box size), else from the size ratio. The car
only stops when BOTH channels are lost; there is no blind coasting.

Protocol: OK locks (windowed votes, sole candidate), Palm/SELECT stop
(latched), involuntary loss opens a re-acquire window (sole returning person
re-locks with a double beep). Buzzer async; RGB unused (I2C crash history).
The mux adds an independent lidar clearance guard + reverse ban on top.
"""
import math
from collections import deque

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from sensor_msgs.msg import Joy, LaserScan
from std_msgs.msg import Bool
from ai_msgs.msg import PerceptionTargets

GESTURE_OK, GESTURE_PALM = 11, 5
# the classifier flickers (11,0,0,11,...), so triggers use windowed votes
OK_WINDOW, OK_HITS = 1.0, 4
PALM_WINDOW, PALM_HITS = 0.7, 2
REACQ_WINDOW = 10.0    # s after involuntary loss: sole person auto re-locks
REACQ_HITS = 8
IMG_W = 960.0
HFOV = math.radians(62)        # horizontal FOV of the 960x544 stream
SHOULDER = 360.0               # px*m: person range ~= SHOULDER / body_width_px

CAM_FRESH = 0.4     # s, camera body observation validity
LEG_FRESH = 1.2     # s, leg track validity (single-leg views flicker)
LOST_UNLOCK = 2.5   # s both channels dark -> brake, unlock, reacq window
PERC_DEAD = 3.0     # s without ANY perception message -> chain dead, stop
SCAN_DEAD = 0.5     # s without /scan while following -> stop

LEG_RANGE = (0.2, 2.4)         # m, cluster range band
LEG_WIDTH = (0.04, 0.40)       # m, physical width of a leg / leg pair
GATE_B_CAM, GATE_R_CAM = 0.5, 0.8   # gates around the camera estimate
GATE_B_LEG, GATE_R_LEG = 0.4, 0.4   # tighter gates around the leg prediction
# Table/chair legs pass the size filter. Legs are told apart by MOTION in the
# odom (world) frame — the robot's own movement cancels out:
#   acquire (camera dark): the cluster must NOT have been at the same world
#   spot MOVE_WIN ago ("it was already there" == furniture);
#   hold (camera dark): a tracked cluster whose world position stays within
#   MOVE_MIN for STATIC_DROP seconds is furniture — drop it.
MOVE_MIN = 0.12     # m
MOVE_WIN = 1.2      # s
STATIC_DROP = 3.0   # s
LEG_GAP = 0.3       # s: track older than this counts as re-acquisition
ODOM_FRESH = 0.5    # s, odom needed for the motion test

# Mecanum: velocity is a VECTOR aimed at the owner (vx=v cos b, vy=v sin b),
# heading alignment runs in parallel via wz — speed direction is never wrong
# even mid-turn. Straight-line speed caps at V_STRAIGHT; the cap grows with
# the bearing (the path is longer when cutting a corner) up to V_DIAG.
V_STRAIGHT, V_DIAG = 0.5, 0.8
BOOST_FULL = 0.6    # rad, bearing at which the cap reaches V_DIAG
W_MAX = 2.5
KV_LEG = 1.5        # m/s per m of lidar distance error
KV_CAM = 1.2        # m/s per unit of log-size error (camera fallback)
KW = 4.0            # rad/s per rad of bearing error
KW_FF = 0.6         # feedforward on the bearing RATE: start swinging the nose
                    # as soon as the owner starts moving sideways, not after
                    # the error has already built up
DEAD_R, DEAD_E, DEAD_B = 0.07, 0.10, 0.03
ACC = 0.8           # m/s^2 speed-magnitude slew limit (stops are immediate)
REF_DIST_MIN, REF_DIST_MAX = 0.45, 1.5
ADOPT_DX = 0.25 * IMG_W        # px gate for id-switch adoption
CAM_MATCH = 0.3                # rad, body-vs-leg bearing gate for adoption
MIN_ROI_H = 12
SELECT_BTN = 6


def rois_of(target):
    return {r.type: r.rect for r in list(target.rois)}


def gestures_of(target):
    return [int(a.value) for a in list(target.attributes)
            if 'gesture' in a.type]


def px_to_bearing(cx):
    """Image column -> signed bearing in the robot frame (left positive)."""
    return -(cx / IMG_W - 0.5) * HFOV


def wrap(a):
    """Shortest signed angle — a person crossing directly behind the car sits
    on the ±pi seam, where naive subtraction breaks gates and velocity."""
    return math.atan2(math.sin(a), math.cos(a))


class FollowMe(Node):
    def __init__(self):
        super().__init__('follow_me')
        self.pub = self.create_publisher(Twist, '/cmd_vel_follow', 10)
        self.buzz = self.create_publisher(Bool, '/Buzzer', 10)
        self.create_subscription(PerceptionTargets, '/hobot_hand_gesture_detection',
                                 self.on_perception, 10)
        self.create_subscription(Joy, '/joy', self.on_joy, 10)
        self.create_subscription(LaserScan, '/scan', self.on_scan,
                                 qos_profile_sensor_data)
        self.create_subscription(Odometry, '/odom', self.on_odom,
                                 qos_profile_sensor_data)
        self.state = 'IDLE'
        self.track_id = -1
        self.refs = {}                     # roi sizes at lock (camera fallback)
        self.ref_dist = None               # follow distance, set by first leg fix
        # camera channel
        self.cam_t = -1.0
        self.cam_bearing, self.cam_range = 0.0, 0.0
        self.cam_cx, self.cam_bw = 0.0, 0.0
        self.cam_rois = {}
        # lidar channel
        self.leg_t = -1.0
        self.leg_bearing, self.leg_range = 0.0, 0.0
        self.leg_vb, self.leg_vr = 0.0, 0.0   # cluster velocity (rad/s, m/s)
        self.scan_t = -1.0
        # odom pose + world-frame motion evidence
        self.odom_t, self.ox, self.oy, self.oyaw = -1.0, 0.0, 0.0, 0.0
        self.hist = deque()        # (t, [(wx,wy) of every cluster]) snapshots
        self.leg_hist = deque()    # (t, wx, wy) of the tracked leg
        self.last_perc = -1.0
        # gesture voting / re-acquire
        self.ok_hits, self.palm_hits, self.seen_hits = {}, [], []
        self.reacq_until = 0.0
        # actuation
        self.b_prev, self.b_prev_t, self.b_rate = 0.0, -1.0, 0.0
        self.v_prev, self.t_prev = 0.0, -1.0
        self.zeros_left = 0
        self.beeps, self.beep_t = [], 0.0
        self.create_timer(0.05, self.tick)
        self.get_logger().info('follow_me up: OK 锁定 / Palm 停止 / SELECT 停止 (相机+雷达融合)')

    def now(self):
        return self.get_clock().now().nanoseconds / 1e9

    # ---- feedback / watchdogs -------------------------------------------
    def beep(self, pattern):
        self.beeps = list(pattern)
        self.beep_t = 0.0

    def tick(self):
        if self.beeps and self.now() >= self.beep_t:
            on, dur = self.beeps.pop(0)
            self.buzz.publish(Bool(data=on))
            self.beep_t = self.now() + dur
        if self.zeros_left > 0:
            self.zeros_left -= 1
            self.pub.publish(Twist())
        if self.state == 'FOLLOW':
            if self.now() - self.last_perc > PERC_DEAD:
                self.stop('感知链断流', lost=True)
            elif self.now() - self.scan_t > SCAN_DEAD:
                self.stop('雷达断流', lost=True)

    def stop(self, why, lost=False):
        self.get_logger().warn(f'停止跟随: {why}')
        # refs/ref_dist survive: the re-acquire path resumes with the same
        # follow distance
        self.state, self.track_id = 'IDLE', -1
        self.ok_hits, self.palm_hits, self.seen_hits = {}, [], []
        self.cam_t = self.leg_t = -1.0
        self.cam_bw = self.leg_vb = self.leg_vr = 0.0
        self.leg_hist.clear()
        self.reacq_until = self.now() + REACQ_WINDOW if lost else 0.0
        self.v_prev, self.zeros_left = 0.0, 10
        self.pub.publish(Twist())
        self.beep([(True, .08), (False, .08)] * 3 if lost
                  else [(True, .6), (False, .05)])

    # ---- camera channel ---------------------------------------------------
    def on_joy(self, msg):
        if self.state != 'IDLE' and len(msg.buttons) > SELECT_BTN \
                and msg.buttons[SELECT_BTN]:
            self.stop('手柄 SELECT')

    def on_perception(self, msg):
        self.last_perc = self.now()
        persons, orphans, palms = {}, [], False
        for tgt in list(msg.targets):
            rois, gests = rois_of(tgt), gestures_of(tgt)
            if [g for g in gests if g]:
                self.get_logger().info(
                    f'手势 {gests} id={tgt.track_id} rois={list(rois)}',
                    throttle_duration_sec=1.0)
            if 'body' in rois:
                persons[tgt.track_id] = [rois, gests]
            elif gests and 'hand' in rois:
                orphans.append((rois['hand'], gests))
            palms = palms or GESTURE_PALM in gests
        # gestures may ride on hand-only targets with their own track id:
        # attach by hand-center-in-body containment
        for hand, gests in orphans:
            hx = hand.x_offset + hand.width / 2.0
            hy = hand.y_offset + hand.height / 2.0
            for rois, g in persons.values():
                b = rois['body']
                if b.x_offset <= hx <= b.x_offset + b.width \
                        and b.y_offset <= hy <= b.y_offset + b.height:
                    g.extend(gests)
                    break
        t = self.now()
        if palms:
            self.palm_hits.append(t)
        self.palm_hits = [x for x in self.palm_hits if t - x < PALM_WINDOW]
        if self.state == 'FOLLOW':
            if len(self.palm_hits) >= PALM_HITS:
                return self.stop('Palm 手势')
            self.update_cam(persons, t)
            self.control(t)     # steer at camera rate (30 Hz), not just lidar
        else:
            if len(self.palm_hits) >= PALM_HITS:
                self.reacq_until = 0.0     # a Palm cancels auto re-acquire
            self.try_lock(persons, t)

    def update_cam(self, persons, t):
        """Refresh the camera observation of the locked person; adopt a new
        track id (turning re-assigns ids) by position or leg-bearing match."""
        if self.track_id not in persons:
            best = None
            for tid, (rois, _) in persons.items():
                b = rois['body']
                cx = b.x_offset + b.width / 2.0
                near_last = self.cam_bw > 0 and abs(cx - self.cam_cx) < ADOPT_DX \
                    and 0.5 <= b.width / self.cam_bw <= 2.0
                near_leg = t - self.leg_t < LEG_FRESH \
                    and abs(px_to_bearing(cx) - self.leg_bearing) < CAM_MATCH
                if near_last or near_leg:
                    d = abs(cx - self.cam_cx)
                    if best is None or d < best[0]:
                        best = (d, tid)
            if best is None:
                return                     # camera dark; the leg track carries on
            self.get_logger().info(f'跟踪ID切换 {self.track_id} -> {best[1]}')
            self.track_id = best[1]
        rois, _ = persons[self.track_id]
        body = rois['body']
        self.cam_t = t
        self.cam_rois = rois
        self.cam_cx = body.x_offset + body.width / 2.0
        self.cam_bw = float(body.width)
        self.cam_bearing = px_to_bearing(self.cam_cx)
        self.cam_range = min(max(SHOULDER / max(self.cam_bw, 1.0), 0.3), 3.0)

    def scale_error(self):
        """log(ref/now): camera-only speed fallback when the leg track is out."""
        for k in ('face', 'head'):
            r = self.cam_rois.get(k)
            if k in self.refs and r and r.height >= MIN_ROI_H and r.y_offset > 2:
                return math.log(self.refs[k] / r.height)
        r = self.cam_rois.get('body')
        if 'body' in self.refs and r and r.width >= MIN_ROI_H:
            return math.log(self.refs['body'] / r.width)
        return None

    # ---- IDLE: arm on a clean OK (or re-acquire after a loss) -------------
    def try_lock(self, persons, t):
        if t < self.reacq_until and (self.refs or self.ref_dist):
            if len(persons) == 1:
                self.seen_hits.append(t)
            self.seen_hits = [x for x in self.seen_hits if t - x < OK_WINDOW]
            if len(self.seen_hits) >= REACQ_HITS and len(persons) == 1:
                self.engage(next(iter(persons)), persons, t, '找回主人')
                return
        for tid, (_, g) in persons.items():
            if GESTURE_OK in g:
                self.ok_hits.setdefault(tid, []).append(t)
        self.ok_hits = {tid: [x for x in xs if t - x < OK_WINDOW]
                        for tid, xs in self.ok_hits.items()}
        armed = [tid for tid, xs in self.ok_hits.items()
                 if len(xs) >= OK_HITS and tid in persons]
        if len(armed) != 1:                # nobody or ambiguous: never guess
            return
        rois, _ = persons[armed[0]]
        refs = {k: float(rois[k].height) for k in ('face', 'head')
                if k in rois and rois[k].height >= MIN_ROI_H}
        if rois['body'].width >= MIN_ROI_H:
            refs['body'] = float(rois['body'].width)
        if not refs:
            return
        self.refs, self.ref_dist = refs, None    # ref_dist from first leg fix
        self.engage(armed[0], persons, t, '锁定')

    def engage(self, tid, persons, t, why):
        self.state, self.track_id = 'FOLLOW', tid
        self.ok_hits, self.seen_hits, self.reacq_until = {}, [], 0.0
        self.leg_vb = self.leg_vr = 0.0
        self.leg_hist.clear()
        self.v_prev, self.t_prev = 0.0, t
        self.update_cam(persons, t)
        self.get_logger().info(f'{why} track {tid}')
        self.beep([(True, .1), (False, .1)] * 2)

    # ---- lidar channel + the single control loop --------------------------
    def on_odom(self, msg):
        self.odom_t = self.now()
        p, q = msg.pose.pose.position, msg.pose.pose.orientation
        self.ox, self.oy = p.x, p.y
        self.oyaw = math.atan2(2 * (q.w * q.z + q.x * q.y),
                               1 - 2 * (q.y * q.y + q.z * q.z))

    def world(self, b, r):
        return (self.ox + r * math.cos(self.oyaw + b),
                self.oy + r * math.sin(self.oyaw + b))

    def was_already_there(self, b, r, t):
        """True if some cluster occupied this world spot MOVE_WIN ago —
        i.e. the candidate is furniture, not a walking leg. With no usable
        history the answer is conservative: treat as static."""
        wx, wy = self.world(b, r)
        found_band = False
        for ht, pts in self.hist:
            if abs((t - ht) - MOVE_WIN) < 0.4:
                found_band = True
                if any(math.hypot(wx - px, wy - py) < MOVE_MIN
                       for px, py in pts):
                    return True
        return not found_band      # no history yet: conservative, reject

    def on_scan(self, msg):
        t = self.now()
        self.scan_t = t
        if self.state != 'FOLLOW':
            return
        legs = self.clusters(msg)
        odom_ok = t - self.odom_t < ODOM_FRESH
        if odom_ok:                        # world snapshot for the motion test
            self.hist.append((t, [self.world(b, r) for b, r in legs]))
            while self.hist and t - self.hist[0][0] > MOVE_WIN + 0.6:
                self.hist.popleft()
        cam_fresh = t - self.cam_t < CAM_FRESH
        # gate the cluster search around the best current estimate; when only
        # the leg track is available, project it forward with its own velocity
        # so a walking person (even one seen as a single leg) stays in the gate
        if cam_fresh:
            cb, cr, gb, gr = (self.cam_bearing, self.cam_range,
                              GATE_B_CAM, GATE_R_CAM)
        elif t - self.leg_t < LEG_FRESH:
            dt = t - self.leg_t
            cb = wrap(self.leg_bearing + self.leg_vb * dt)
            cr = self.leg_range + self.leg_vr * dt
            gb, gr = GATE_B_LEG, GATE_R_LEG
        else:
            self.control(t)
            return
        best = None
        for b, r in legs:
            db, dr = abs(wrap(b - cb)), abs(r - cr)
            if db < gb and dr < gr and (best is None or db + dr < best[0]):
                best = (db + dr, b, r)
        # camera dark + (re)acquiring: only a cluster that MOVED can be the
        # owner — furniture legs pass the size filter but never the motion test
        if best and not cam_fresh and t - self.leg_t >= LEG_GAP:
            if not odom_ok or self.was_already_there(best[1], best[2], t):
                best = None
        if best:
            _, b, r = best
            dt = t - self.leg_t
            if 0.0 < dt < 0.5:             # low-passed cluster velocity
                self.leg_vb = max(-2.0, min(2.0,
                    0.6 * self.leg_vb + 0.4 * wrap(b - self.leg_bearing) / dt))
                self.leg_vr = max(-1.5, min(1.5,
                    0.6 * self.leg_vr + 0.4 * (r - self.leg_range) / dt))
            self.leg_bearing, self.leg_range, self.leg_t = b, r, t
            if self.ref_dist is None:
                self.ref_dist = min(max(self.leg_range, REF_DIST_MIN), REF_DIST_MAX)
                self.get_logger().info(f'跟随距离基准 {self.ref_dist:.2f}m (雷达)')
            # camera dark: a tracked "leg" whose WORLD position never moves is
            # furniture we grabbed by mistake — let go and stand down
            if odom_ok:
                wx, wy = self.world(b, r)
                self.leg_hist.append((t, wx, wy))
                while self.leg_hist and t - self.leg_hist[0][0] > STATIC_DROP + 0.5:
                    self.leg_hist.popleft()
                if not cam_fresh \
                        and t - self.leg_hist[0][0] >= STATIC_DROP \
                        and all(math.hypot(wx - hx, wy - hy) < MOVE_MIN
                                for _, hx, hy in self.leg_hist):
                    self.get_logger().warn('腿目标世界系静止,疑似家具,放弃')
                    self.leg_t = -1.0
                    self.leg_hist.clear()
        self.control(t)

    def clusters(self, msg):
        """Leg-sized scan clusters as (signed bearing, range), full 360 deg.
        The first and last groups are merged when they touch across the ±pi
        seam — that seam is directly BEHIND the car, exactly where a circling
        person used to drop off the track."""
        pts = []
        for i, r in enumerate(msg.ranges):
            if LEG_RANGE[0] < r < LEG_RANGE[1]:
                a = msg.angle_min + i * msg.angle_increment
                pts.append((a if a < math.pi else a - 2 * math.pi, r))
        pts.sort()
        groups, cur = [], []
        for p in pts:
            if cur and (p[0] - cur[-1][0] > 0.06 or abs(p[1] - cur[-1][1]) > 0.10):
                groups.append(cur)
                cur = []
            cur.append(p)
        if cur:
            groups.append(cur)
        if len(groups) > 1:
            first, last = groups[0], groups[-1]
            if first[0][0] + 2 * math.pi - last[-1][0] < 0.06 \
                    and abs(first[0][1] - last[-1][1]) < 0.10:
                groups[0] = [(a - 2 * math.pi, r) for a, r in last] + first
                groups.pop()
        legs = []
        for c in groups:
            rng = sum(p[1] for p in c) / len(c)
            if LEG_WIDTH[0] <= (c[-1][0] - c[0][0]) * rng <= LEG_WIDTH[1]:
                legs.append((wrap((c[0][0] + c[-1][0]) / 2), rng))
        return legs

    def control(self, t):
        cam = t - self.cam_t < CAM_FRESH
        leg = t - self.leg_t < LEG_FRESH
        if not cam and not leg:
            dark = t - max(self.cam_t, self.leg_t)
            if dark > LOST_UNLOCK or self.cam_t < 0:
                self.stop('目标丢失(双通道)', lost=True)
            else:
                self.pub.publish(Twist())  # both channels dark: stand still
            return
        bearing = self.cam_bearing if cam else self.leg_bearing
        # bearing rate (low-passed) feeds forward into steering: the nose
        # starts swinging the moment the owner starts crossing, which is what
        # keeps wide turns inside the camera's field of view
        dtb = t - self.b_prev_t
        if 0.01 < dtb < 0.5:
            self.b_rate = 0.7 * self.b_rate + 0.3 * wrap(bearing - self.b_prev) / dtb
            self.b_prev, self.b_prev_t = bearing, t
        elif dtb >= 0.5:
            self.b_rate, self.b_prev, self.b_prev_t = 0.0, bearing, t
        wz = KW * bearing + KW_FF * self.b_rate if abs(bearing) > DEAD_B \
            else KW_FF * self.b_rate
        if leg and self.ref_dist is not None:
            e = self.leg_range - self.ref_dist
            v = KV_LEG * e if e > DEAD_R else 0.0
        else:
            es = self.scale_error() if cam else None
            v = KV_CAM * es if es is not None and es > DEAD_E else 0.0
        cap = V_STRAIGHT + (V_DIAG - V_STRAIGHT) * min(abs(bearing) / BOOST_FULL, 1.0)
        dt = max(t - self.t_prev, 1e-3)
        v = min(v, self.v_prev + ACC * dt, cap)
        self.v_prev, self.t_prev = max(v, 0.0), t
        cmd = Twist()
        cmd.linear.x = self.v_prev * math.cos(bearing)   # velocity vector aims
        cmd.linear.y = self.v_prev * math.sin(bearing)   # at the owner (mecanum)
        cmd.angular.z = max(-W_MAX, min(W_MAX, wz))
        self.pub.publish(cmd)


def main():
    rclpy.init()
    rclpy.spin(FollowMe())


if __name__ == '__main__':
    main()
