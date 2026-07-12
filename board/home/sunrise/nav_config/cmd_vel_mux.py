#!/usr/bin/env python3
"""cmd_vel priority mux + watchdog + follow guard (seed of cmd_vel_guard).

  /cmd_vel_joy    (HIGH, joystick)       \
  /cmd_vel_follow (MID,  follow-me)       > /cmd_vel_drv -> Mcnamu_driver
  /cmd_vel        (LOW,  Nav2/keyboard)  /

Rules:
- A message from a source is forwarded unless a higher-priority source has
  spoken within the last HOLD seconds.
- Watchdog: if every source goes silent for TIMEOUT, keep publishing zero
  Twist every tick. Continuous (not one-shot) zeros matter: if the driver
  crashes and respawns, the fresh instance immediately receives a stop
  instead of the MCU running the last non-zero command forever
  (real incident: vendor driver died in an unguarded I2C write while moving).
- Follow guard: /cmd_vel_follow is the only source with neither a human in
  the loop nor its own obstacle avoidance (Nav2 has costmaps, joy/keys have
  eyes). The follower drives the mecanum base as a velocity VECTOR (vx, vy),
  so the guard checks the lidar sector AROUND THE MOTION DIRECTION: speed is
  limited in proportion to the clearance found there, and a sector with no
  valid return counts as blocked (the MS200 reports 0.0 in its dead zone and
  on absorbing surfaces). vx < 0 stays denied. Lidar yaw is 0 vs base_link.
"""
import math

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from geometry_msgs.msg import Twist
from sensor_msgs.msg import LaserScan

HOLD = 0.5        # a source keeps control this long after its last message
TIMEOUT = 0.5     # total command silence -> brake
SCAN_FRESH = 0.4  # /scan older than this -> follow may not move forward
FRONT_STOP = 0.35             # m, clearance below this stops follow forward
CLEAR_GAIN = 0.3              # s, speed limit = (clearance - FRONT_STOP) / this
FRONT_HALF = math.radians(30) # half-angle of the guarded front sector
RANGE_VALID = 0.05            # ranges <= this are sensor artifacts (0.0 / nan)


class CmdVelMux(Node):
    def __init__(self):
        super().__init__('cmd_vel_mux')
        self.pub = self.create_publisher(Twist, '/cmd_vel_drv', 10)
        for prio, topic in enumerate(['/cmd_vel_joy', '/cmd_vel_follow', '/cmd_vel']):
            self.create_subscription(
                Twist, topic, lambda m, p=prio: self.arbitrate(p, m), 10)
        self.create_subscription(LaserScan, '/scan', self.on_scan,
                                 qos_profile_sensor_data)
        self.last = [-1.0, -1.0, -1.0]   # last message time per priority
        self.last_fwd = -1.0
        self.scan_t = -1.0
        self.scan = None
        self.create_timer(0.1, self.watchdog)

    def now(self):
        return self.get_clock().now().nanoseconds / 1e9

    def on_scan(self, msg):
        self.scan_t = self.now()
        self.scan = msg

    def sector_min(self, direction):
        """Min valid range within ±FRONT_HALF of `direction` (radians, robot
        frame). A sector with no valid return is BLOCKED, not clear: the
        MS200 reports 0.0 in its ~0.1 m dead zone and on absorbing surfaces."""
        best = math.inf
        for i, r in enumerate(self.scan.ranges):
            a = self.scan.angle_min + i * self.scan.angle_increment - direction
            a = math.atan2(math.sin(a), math.cos(a))
            if abs(a) <= FRONT_HALF and RANGE_VALID < r < best:
                best = r
        return best if best < math.inf else 0.0

    def guard(self, msg):
        if self.now() - self.scan_t > SCAN_FRESH or self.scan is None:
            return Twist()               # blind: no autonomous motion at all
        vx, vy = max(msg.linear.x, 0.0), msg.linear.y   # never reverse
        speed = math.hypot(vx, vy)
        if speed > 0.01:
            # clearance-proportional speed limit along the motion direction:
            # 0 at FRONT_STOP, +1 m/s per CLEAR_GAIN meters of margin
            clear = self.sector_min(math.atan2(vy, vx))
            allowed = max((clear - FRONT_STOP) / CLEAR_GAIN, 0.0)
            if speed > allowed:
                vx, vy = vx * allowed / speed, vy * allowed / speed
        msg.linear.x, msg.linear.y = vx, vy
        return msg

    def arbitrate(self, prio, msg):
        t = self.now()
        if any(t - self.last[p] < HOLD for p in range(prio)):
            return                       # a higher-priority source owns the bus
        if prio == 1:
            msg = self.guard(msg)
        self.last[prio] = self.last_fwd = t
        self.pub.publish(msg)

    def watchdog(self):
        if self.now() - self.last_fwd > TIMEOUT:
            self.pub.publish(Twist())    # keep streaming zeros while idle


def main():
    rclpy.init()
    rclpy.spin(CmdVelMux())


if __name__ == '__main__':
    main()
