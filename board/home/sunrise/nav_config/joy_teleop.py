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
  buttons[0] A key -> toggle the lidar safety brake (/safety_toggle;
          the MODE key is a pad-local analog/digital hardware switch that
          never reaches /joy AND remaps the sticks — unusable, do not press;
          buttons[6] is L1, not SELECT as the vendor code suggested)
  buttons[4] Y key -> stop-all, same as the GUI stop button: cancel the
          Nav2 goal, hold the bus with zeros for ~2 s, and stop a running
          episode recording (/record_stop, idempotent — never
          /record_toggle, which would START one when idle). Feature
          switches (follow-me) are deliberately NOT touched.
  buttons[RECORD_BTN] START key -> toggle episode recording (/record_toggle)
  axes[4]/axes[5] are trigger axes that REST at +1.0 — never map them;
          doing so commands a permanent creep (found the hard way).
"""
import rclpy
from rclpy.node import Node
from action_msgs.srv import CancelGoal
from geometry_msgs.msg import Twist
from sensor_msgs.msg import Joy
from std_msgs.msg import Bool, Empty

DEADZONE = 0.2      # vendor value; also swallows drift below it
ZERO_BURST = 3      # stop twists sent after release before going silent
VX_MAX = 1.0        # m/s, vendor defaults the user has been driving with
VY_MAX = 1.0
WZ_MAX = 5.0        # rad/s
DPAD_V = 0.5        # m/s, D-pad nudge: quicker than the old 0.3 crawl but
                    # still half of full stick — precision positioning speed
KEY_V = 0.3         # m/s, fixed strafe speed for the X/B keys
SAFETY_BTN = 0      # A key: toggle the safety brake (rising edge)
STOP_BTN = 4        # Y key: stop-all (rising edge)
RECORD_BTN = 11     # START key: toggle episode recording (rising edge).
                    # Captured on-board 2026-07-13 — this pad never follows
                    # convention (L1 was mislabeled SELECT).
STOP_ZEROS = 40     # ~2 s of zeros at the 20 Hz joy autorepeat: holds the
                    # mux top priority while the Nav2 cancel takes effect


class JoyTeleop(Node):
    def __init__(self):
        super().__init__('joy_teleop')
        self.pub = self.create_publisher(Twist, '/cmd_vel_joy', 10)
        self.pub_toggle = self.create_publisher(Empty, '/safety_toggle', 10)
        self.pub_rec_toggle = self.create_publisher(Empty, '/record_toggle', 10)
        self.pub_rec_stop = self.create_publisher(Empty, '/record_stop', 10)
        self.cancel_cli = self.create_client(
            CancelGoal, '/navigate_to_pose/_action/cancel_goal')
        self.pub_beep = self.create_publisher(Bool, '/Buzzer', 10)
        self.beep_timer = None
        self.create_subscription(Joy, '/joy', self.on_joy, 10)
        self.zeros_left = 0
        self.safety_btn_prev = 0
        self.stop_btn_prev = 0
        self.rec_btn_prev = 1   # require a seen release before the first
                                # trigger (guards a held key at startup)

    def stop_all(self):
        """Same contract as the GUI stop button: interrupt current MOTION
        (cancel the Nav2 goal, hold the bus with zeros, end a recording) —
        never touch feature switches. follow-me keeps running; the 2 s zero
        burst outranks it and its own Palm gesture / GUI switch turn it off.
        """
        self.zeros_left = STOP_ZEROS
        self.pub.publish(Twist())
        if self.cancel_cli.service_is_ready():
            self.cancel_cli.call_async(CancelGoal.Request())  # cancel all goals
        self.pub_rec_stop.publish(Empty())   # idempotent; a stopped episode
                                             # gets stopped_by: stop_all
        self.pub_beep.publish(Bool(data=True))   # one long beep = stop-all
        if self.beep_timer:
            self.beep_timer.cancel()
        self.beep_timer = self.create_timer(0.6, self.beep_off)
        self.get_logger().info('stop-all: goals cancelled, braking')

    def beep_off(self):
        self.beep_timer.cancel()
        self.pub_beep.publish(Bool(data=False))

    def on_joy(self, joy):
        if len(joy.axes) < 8 or len(joy.buttons) < 7:
            return
        if joy.buttons[SAFETY_BTN] and not self.safety_btn_prev:
            self.pub_toggle.publish(Empty())
        self.safety_btn_prev = joy.buttons[SAFETY_BTN]
        if joy.buttons[STOP_BTN] and not self.stop_btn_prev:
            self.stop_all()
        self.stop_btn_prev = joy.buttons[STOP_BTN]
        if len(joy.buttons) > RECORD_BTN:
            if joy.buttons[RECORD_BTN] and not self.rec_btn_prev:
                self.pub_rec_toggle.publish(Empty())
            self.rec_btn_prev = joy.buttons[RECORD_BTN]
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
