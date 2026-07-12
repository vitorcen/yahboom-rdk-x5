#!/usr/bin/env python3
"""Follow-me: vision-only person following, MVP after external review.

Two states, both stops latched (re-arm only by a fresh OK gesture):
  IDLE   -> OK gesture (11) held LOCK_FRAMES frames by exactly one person
  FOLLOW -> Palm (5) by anyone | joystick SELECT | target ID lost | stale
            perception  ==> zero-velocity burst, back to IDLE

Design decisions (see docs/rdk-x5-follow-me-vision.html):
- Single input topic: /hobot_hand_gesture_detection carries the merged
  PerceptionTargets (body/head/face/hand rois + track ids + gesture attrs).
- No color re-identification, no SEARCH state, no metric distance: lock-time
  roi sizes are the reference, control error is log(ref/now) so "smaller than
  reference" means "farther, go". Scale source degrades face -> head -> body
  width; a face touching the image top edge is cropped, hence invalid.
- Steering uses the body-box center (faces vanish on profile views).
- Forward only; large lateral error means turn in place. The lidar guard and
  reverse ban live in cmd_vel_mux, independent of this process.
- Perception freshness judged by receive time each tick; >PERC_STALE in
  FOLLOW is a fault stop. Stops bypass the acceleration limiter.
- Buzzer (std_msgs/Bool) is fire-and-forget, sequenced by a timer, never
  blocking control. RGB deliberately unused: RGBLightcallback does unguarded
  I2C writes and has crashed the vendor driver before.
"""
import math

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from sensor_msgs.msg import Joy
from std_msgs.msg import Bool
from ai_msgs.msg import PerceptionTargets

GESTURE_OK, GESTURE_PALM = 11, 5
LOCK_FRAMES = 3        # consecutive OK frames required to lock
PALM_FRAMES = 2        # consecutive Palm frames required to stop
LOST_UNLOCK = 0.6      # s target absent -> unlock (zeros stream meanwhile)
PERC_STALE = 0.3       # s without any perception message in FOLLOW -> fault
IMG_W, IMG_H = 960.0, 544.0
V_MAX, W_MAX = 0.25, 1.0
KV, KW = 0.5, 1.6
DEAD_E, DEAD_X = 0.10, 0.04    # scale / lateral deadbands
TURN_ONLY = 0.18       # |cx-0.5| beyond this: rotate in place
ACC = 0.3              # m/s^2 forward slew limit (stops are immediate)
MIN_ROI_H = 12         # px, smaller rois are noise
SELECT_BTN = 6


def rois_of(target):
    d = {}
    for r in list(target.rois):
        d[r.type] = r.rect             # sensor_msgs/RegionOfInterest
    return d


def gestures_of(target):
    return [int(a.value) for a in list(target.attributes)
            if 'gesture' in a.type]


class FollowMe(Node):
    def __init__(self):
        super().__init__('follow_me')
        self.pub = self.create_publisher(Twist, '/cmd_vel_follow', 10)
        self.buzz = self.create_publisher(Bool, '/Buzzer', 10)
        self.create_subscription(PerceptionTargets, '/hobot_hand_gesture_detection',
                                 self.on_perception, 10)
        self.create_subscription(Joy, '/joy', self.on_joy, 10)
        self.state = 'IDLE'
        self.track_id = -1
        self.refs = {}                 # roi type -> reference size at lock
        self.ok_frames = {}            # track id -> consecutive OK frames
        self.palm_frames = 0
        self.last_perc = -1.0
        self.last_seen = -1.0
        self.v_prev, self.t_prev = 0.0, -1.0
        self.zeros_left = 0            # post-stop zero burst, sent by ticker
        self.beeps = []                # [(bool, seconds), ...] timer-driven
        self.beep_t = 0.0
        self.create_timer(0.05, self.tick)
        self.get_logger().info('follow_me up: OK 锁定 / Palm 停止 / SELECT 停止')

    def now(self):
        return self.get_clock().now().nanoseconds / 1e9

    # ---- feedback ------------------------------------------------------
    def beep(self, pattern):
        self.beeps = list(pattern)     # replace, never queue up
        self.beep_t = 0.0

    def tick(self):
        # buzzer sequencer
        if self.beeps and self.now() >= self.beep_t:
            on, dur = self.beeps.pop(0)
            self.buzz.publish(Bool(data=on))
            self.beep_t = self.now() + dur
        # post-stop zero burst keeps the mux fed until it takes over
        if self.zeros_left > 0:
            self.zeros_left -= 1
            self.pub.publish(Twist())
        # perception watchdog
        if self.state == 'FOLLOW' and self.now() - self.last_perc > PERC_STALE:
            self.stop('感知超时', lost=True)

    def stop(self, why, lost=False):
        self.get_logger().warn(f'停止跟随: {why}')
        self.state, self.track_id, self.refs = 'IDLE', -1, {}
        self.ok_frames, self.palm_frames = {}, 0
        self.v_prev, self.zeros_left = 0.0, 10
        self.pub.publish(Twist())      # immediate, bypasses everything
        self.beep([(True, .08), (False, .08)] * 3 if lost
                  else [(True, .6), (False, .05)])

    # ---- inputs --------------------------------------------------------
    def on_joy(self, msg):
        if self.state == 'FOLLOW' and len(msg.buttons) > SELECT_BTN \
                and msg.buttons[SELECT_BTN]:
            self.stop('手柄 SELECT')

    def on_perception(self, msg):
        self.last_perc = self.now()
        persons, orphans, palms = {}, [], False
        for t in list(msg.targets):
            rois, gests = rois_of(t), gestures_of(t)
            if gests:
                self.get_logger().info(
                    f'手势 {gests} type={t.type} id={t.track_id} rois={list(rois)}',
                    throttle_duration_sec=1.0)
            if 'body' in rois:
                persons[t.track_id] = [rois, gests]
            elif gests and 'hand' in rois:
                orphans.append((rois['hand'], gests))
            palms = palms or GESTURE_PALM in gests
        # the pipeline may emit gestures on hand-only targets whose track_id
        # differs from the person's: attach by hand-center-in-body containment
        for hand, gests in orphans:
            hx = hand.x_offset + hand.width / 2.0
            hy = hand.y_offset + hand.height / 2.0
            for rois, g in persons.values():
                b = rois['body']
                if b.x_offset <= hx <= b.x_offset + b.width \
                        and b.y_offset <= hy <= b.y_offset + b.height:
                    g.extend(gests)
                    break
        if palms:
            self.palm_frames += 1
        else:
            self.palm_frames = 0
        if self.state == 'FOLLOW':
            if self.palm_frames >= PALM_FRAMES:
                return self.stop('Palm 手势')
            self.follow(persons)
        else:
            self.try_lock(persons)

    # ---- IDLE: arm on a clean OK ---------------------------------------
    def try_lock(self, persons):
        oks = {tid for tid, (_, g) in persons.items() if GESTURE_OK in g}
        self.ok_frames = {tid: self.ok_frames.get(tid, 0) + 1 for tid in oks}
        if len(oks) != 1:              # nobody or ambiguous: never guess
            return
        tid = next(iter(oks))
        if self.ok_frames[tid] < LOCK_FRAMES:
            return
        rois, _ = persons[tid]
        refs = {k: float(rois[k].height) for k in ('face', 'head')
                if k in rois and rois[k].height >= MIN_ROI_H}
        if rois['body'].width >= MIN_ROI_H:
            refs['body'] = float(rois['body'].width)
        if not refs:
            return
        self.state, self.track_id, self.refs = 'FOLLOW', tid, refs
        self.last_seen, self.v_prev, self.t_prev = self.now(), 0.0, self.now()
        self.ok_frames = {}
        self.get_logger().info(f'锁定 track {tid}, 基准 {self.refs}')
        self.beep([(True, .1), (False, .1)] * 2)

    # ---- FOLLOW: one control step per perception frame ------------------
    def scale_error(self, rois):
        """log(ref/now) from the best available roi; None = no valid scale."""
        for k in ('face', 'head'):
            r = rois.get(k)
            if k in self.refs and r and r.height >= MIN_ROI_H \
                    and r.y_offset > 2:            # touching top edge = cropped
                return math.log(self.refs[k] / r.height)
        r = rois.get('body')
        if 'body' in self.refs and r and r.width >= MIN_ROI_H:
            return math.log(self.refs['body'] / r.width)
        return None

    def follow(self, persons):
        t = self.now()
        if self.track_id not in persons:
            if t - self.last_seen > LOST_UNLOCK:
                self.stop('目标丢失', lost=True)
            else:
                self.pub.publish(Twist())          # blind: stand still
            return
        self.last_seen = t
        rois, _ = persons[self.track_id]
        body = rois['body']
        ex = (body.x_offset + body.width / 2.0) / IMG_W - 0.5
        wz = -KW * ex if abs(ex) > DEAD_X else 0.0
        e = self.scale_error(rois)
        v = KV * e if e is not None and e > DEAD_E else 0.0
        v *= max(0.0, 1.0 - abs(ex) / TURN_ONLY)   # off-axis: slow, then spin
        dt = max(t - self.t_prev, 1e-3)
        v = min(v, self.v_prev + ACC * dt, V_MAX)  # slew up only; down is free
        self.v_prev, self.t_prev = max(v, 0.0), t
        cmd = Twist()
        cmd.linear.x = self.v_prev
        cmd.angular.z = max(-W_MAX, min(W_MAX, wz))
        self.pub.publish(cmd)


def main():
    rclpy.init()
    rclpy.spin(FollowMe())


if __name__ == '__main__':
    main()
