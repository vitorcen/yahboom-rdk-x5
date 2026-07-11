#!/usr/bin/env python3
"""cmd_vel priority mux + watchdog (seed of cmd_vel_guard).

  /cmd_vel_joy (prio HIGH, joystick) \
                                      > /cmd_vel_drv -> Mcnamu_driver
  /cmd_vel     (prio LOW, Nav2/keys) /

Rules:
- Joystick message => forward it and mute Nav2 for HOLD seconds.
- Nav2 forwarded only while joystick is quiet.
- Watchdog: if the active source goes silent for TIMEOUT, keep publishing
  zero Twist every tick. Continuous (not one-shot) zeros matter: if the
  driver crashes and respawns, the fresh instance immediately receives a
  stop instead of the MCU running the last non-zero command forever
  (real incident: vendor driver died in an unguarded I2C write while moving).
"""
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist

HOLD = 0.5      # joystick keeps control this long after last input
TIMEOUT = 0.5   # silence -> brake


class CmdVelMux(Node):
    def __init__(self):
        super().__init__('cmd_vel_mux')
        self.pub = self.create_publisher(Twist, '/cmd_vel_drv', 10)
        self.create_subscription(Twist, '/cmd_vel_joy', self.on_joy, 10)
        self.create_subscription(Twist, '/cmd_vel', self.on_nav, 10)
        self.last_joy = self.last_fwd = -1.0
        self.create_timer(0.1, self.watchdog)

    def now(self):
        return self.get_clock().now().nanoseconds / 1e9

    def on_joy(self, msg):
        self.last_joy = self.last_fwd = self.now()
        self.pub.publish(msg)

    def on_nav(self, msg):
        t = self.now()
        if t - self.last_joy < HOLD:
            return                      # joystick owns the chassis
        self.last_fwd = t
        self.pub.publish(msg)

    def watchdog(self):
        if self.now() - self.last_fwd > TIMEOUT:
            self.pub.publish(Twist())   # keep streaming zeros while idle


def main():
    rclpy.init()
    rclpy.spin(CmdVelMux())


if __name__ == '__main__':
    main()
