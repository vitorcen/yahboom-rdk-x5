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
  eyes), so forward motion requires a fresh /scan AND a clear front sector;
  reverse is always denied. Lidar yaw is 0 vs base_link, so scan angle 0 is
  straight ahead and the front sector wraps across the 0/2pi seam.
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
FRONT_STOP = 0.35             # m, required clearance ahead for follow forward
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
        self.front_min = math.inf
        self.create_timer(0.1, self.watchdog)

    def now(self):
        return self.get_clock().now().nanoseconds / 1e9

    def on_scan(self, msg):
        self.scan_t = self.now()
        best = math.inf
        for i, r in enumerate(msg.ranges):
            a = msg.angle_min + i * msg.angle_increment
            if min(a, 2 * math.pi - a) <= FRONT_HALF \
                    and RANGE_VALID < r < best:
                best = r
        # A front sector with no valid return is NOT clear: the MS200 reports
        # 0.0 both inside its ~0.1 m dead zone and on absorbing surfaces.
        self.front_min = best if best < math.inf else 0.0

    def guard(self, msg):
        self.get_logger().info(
            f'guard vx={msg.linear.x:.2f} scan_age={self.now()-self.scan_t:.2f} '
            f'front={self.front_min:.2f}', throttle_duration_sec=1.0)
        if self.now() - self.scan_t > SCAN_FRESH:
            return Twist()               # blind: no autonomous motion at all
        if msg.linear.x > 0.0 and self.front_min < FRONT_STOP:
            msg.linear.x = 0.0           # blocked ahead: rotation only
        msg.linear.x = max(msg.linear.x, 0.0)   # follow never reverses
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
