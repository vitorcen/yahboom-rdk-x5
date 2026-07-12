#!/usr/bin/env python3
"""Stateless joystick teleop: /joy -> /cmd_vel_joy (highest mux priority).

Replaces vendor yahboom_joy, whose Joy_active latch fails both ways:
- defaults OFF and resets on every restart, so the gamepad is silently
  dead until an obscure unlock button (buttons[6]) is pressed;
- once ON it publishes a twist on EVERY /joy message (joy_node autorepeats
  at 20 Hz even with sticks centered), so /cmd_vel_joy streams zeros
  forever and the mux HOLD starves follow-me and Nav2 permanently.

No latch, no state: priority is claimed by moving a stick and released by
letting go. On release a short zero burst stops the car immediately, then
silence hands the bus back to lower-priority sources after the mux HOLD
(0.5 s).

Mapping for this receiver (0079:181c, 8 axes), every line below measured
on-board with multi-second key holds — never trust convention here:
  axes[1] left stick fwd/back   -> linear.x
  axes[0] left stick left/right -> angular.z (turn)
  axes[2] right stick left/right-> linear.y (strafe)
  axes[7]/axes[6] D-pad hat (up/left = +1) -> slow fwd/strafe nudge
  buttons[3] X key -> strafe LEFT at KEY_V   (operator request)
  buttons[1] B key -> strafe RIGHT at KEY_V
  axes[4]/axes[5] are trigger axes that REST at +1.0 — never map them;
          doing so commands a permanent creep (found the hard way).
"""
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from sensor_msgs.msg import Joy

DEADZONE = 0.2      # vendor value; also swallows drift below it
ZERO_BURST = 3      # stop twists sent after release before going silent
VX_MAX = 1.0        # m/s, vendor defaults the user has been driving with
VY_MAX = 1.0
WZ_MAX = 5.0        # rad/s
DPAD_V = 0.3        # m/s, slow nudge speed for the D-pad hat
KEY_V = 0.3         # m/s, fixed strafe speed for the X/B keys


class JoyTeleop(Node):
    def __init__(self):
        super().__init__('joy_teleop')
        self.pub = self.create_publisher(Twist, '/cmd_vel_joy', 10)
        self.create_subscription(Joy, '/joy', self.on_joy, 10)
        self.zeros_left = 0

    def on_joy(self, joy):
        if len(joy.axes) < 8 or len(joy.buttons) < 4:
            return
        t = Twist()
        t.linear.x = self.scale(joy.axes[1], VX_MAX) + joy.axes[7] * DPAD_V
        t.linear.y = (self.scale(joy.axes[2], VY_MAX)
                      + joy.axes[6] * DPAD_V
                      + (joy.buttons[3] - joy.buttons[1]) * KEY_V)  # X left, B right
        t.angular.z = self.scale(joy.axes[0], WZ_MAX)
        if t.linear.x or t.linear.y or t.angular.z:
            self.zeros_left = ZERO_BURST
            self.pub.publish(t)
        elif self.zeros_left > 0:
            self.zeros_left -= 1
            self.pub.publish(Twist())

    @staticmethod
    def scale(axis, limit):
        if abs(axis) < DEADZONE:
            return 0.0
        return max(-limit, min(limit, axis * limit))


def main():
    rclpy.init()
    rclpy.spin(JoyTeleop())


if __name__ == '__main__':
    main()
