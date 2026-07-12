// safety_stop: last-resort lidar brake between cmd_vel_mux and the driver.
//
//   mux -> /cmd_vel_mux -> [this node] -> /cmd_vel_drv -> Mcnamu_driver
//
// Event-driven pass-through: every incoming Twist is forwarded immediately,
// with (vx, vy) clamped to a clearance-proportional speed limit measured in
// the lidar sector AROUND THE MOTION DIRECTION: zero at stop_dist, growing
// by 1 m/s per clear_gain meters of margin. A fixed stop distance cannot
// work at every speed (10 Hz scan latency + braking glide overshoot it at
// full stick), the proportional clamp brakes early exactly when fast.
// It filters the final mux output, so joystick, follow-me and Nav2 are all
// covered, in every direction the MS200 sees (360°): forward checks front,
// reverse checks rear, strafe checks the side. Motion away from an obstacle
// checks its own (clear) sector and stays allowed — a human can always back
// out. Rotation (wz) passes untouched: it cannot close distance.
//
// Fail-open on stale/missing scan: the joystick must survive a dead lidar.
// The only source with no human and no costmap (follow-me) already
// fail-closes inside the mux guard.
//
// Runtime switch: an Empty on /safety_toggle flips the guard (boot default
// ON — the node owns the state, GUI and gamepad both just send toggles and
// mirror the latched /safety_enabled broadcast). State changes chirp the
// chassis buzzer via the driver's /Buzzer topic: two short beeps = armed,
// one long beep = disarmed.
//
// A sector with no valid return counts as blocked, same as the mux guard:
// the MS200 reports 0.0 inside its ~0.1 m dead zone and on absorbing
// surfaces.
#include <chrono>
#include <cmath>
#include <utility>
#include <vector>

#include "geometry_msgs/msg/twist.hpp"
#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/laser_scan.hpp"
#include "std_msgs/msg/bool.hpp"
#include "std_msgs/msg/empty.hpp"

using geometry_msgs::msg::Twist;
using sensor_msgs::msg::LaserScan;
using std_msgs::msg::Bool;
using std_msgs::msg::Empty;

class SafetyStop : public rclcpp::Node {
public:
  SafetyStop() : Node("safety_stop") {
    stop_dist_ = declare_parameter("stop_dist", 0.30);
    clear_gain_ = declare_parameter("clear_gain", 0.5);
    sector_half_ = declare_parameter("sector_half", M_PI / 6);
    scan_fresh_ = declare_parameter("scan_fresh", 0.5);
    pub_ = create_publisher<Twist>("/cmd_vel_drv", 10);
    sub_cmd_ = create_subscription<Twist>(
        "/cmd_vel_mux", 10,
        [this](Twist::UniquePtr m) { on_cmd(std::move(m)); });
    sub_scan_ = create_subscription<LaserScan>(
        "/scan", rclcpp::SensorDataQoS(), [this](LaserScan::SharedPtr m) {
          scan_ = std::move(m);
          scan_t_ = now();
        });
    pub_state_ =
        create_publisher<Bool>("/safety_enabled", rclcpp::QoS(1).transient_local());
    pub_beep_ = create_publisher<Bool>("/Buzzer", 10);
    sub_toggle_ = create_subscription<Empty>(
        "/safety_toggle", 10,
        [this](Empty::SharedPtr) { set_enabled(!enabled_); });
    Bool s;
    s.data = enabled_;
    pub_state_->publish(s);  // boot default ON, latched for late joiners
  }

private:
  // Min valid range within ±sector_half of `dir` (radians, robot frame).
  // No valid return in the sector -> 0.0 (blocked, not clear).
  double sector_min(double dir) const {
    double best = INFINITY;
    for (size_t i = 0; i < scan_->ranges.size(); ++i) {
      double a = scan_->angle_min + i * scan_->angle_increment - dir;
      a = std::atan2(std::sin(a), std::cos(a));
      const double r = scan_->ranges[i];
      if (std::abs(a) <= sector_half_ && r > RANGE_VALID && r < best) best = r;
    }
    return std::isinf(best) ? 0.0 : best;
  }

  void set_enabled(bool on) {
    enabled_ = on;
    Bool s;
    s.data = on;
    pub_state_->publish(s);
    RCLCPP_INFO(get_logger(), "safety guard %s", on ? "ARMED" : "disarmed");
    // armed: two short chirps; disarmed: one long beep
    beep(on ? std::vector<std::pair<bool, int>>{{true, 120}, {false, 120},
                                                {true, 120}, {false, 100}}
            : std::vector<std::pair<bool, int>>{{true, 600}, {false, 100}});
  }

  // Play a (buzzer-on?, hold-ms) sequence via one-shot timer chaining.
  void beep(std::vector<std::pair<bool, int>> pattern) {
    pattern_ = std::move(pattern);
    step_ = 0;
    beep_step();
  }

  void beep_step() {
    if (beep_timer_) beep_timer_->cancel();
    if (step_ >= pattern_.size()) return;
    Bool b;
    b.data = pattern_[step_].first;
    pub_beep_->publish(b);
    beep_timer_ = create_wall_timer(
        std::chrono::milliseconds(pattern_[step_++].second),
        [this] { beep_step(); });
  }

  void on_cmd(Twist::UniquePtr msg) {
    const double speed = std::hypot(msg->linear.x, msg->linear.y);
    if (enabled_ && speed > 0.01) {
      if (!scan_ || (now() - scan_t_).seconds() > scan_fresh_) {
        RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 5000,
                             "scan stale - passing commands unguarded");
      } else {
        const double clear =
            sector_min(std::atan2(msg->linear.y, msg->linear.x));
        const double allowed =
            std::max((clear - stop_dist_) / clear_gain_, 0.0);
        if (speed > allowed) {
          const double k = allowed / speed;
          msg->linear.x *= k;
          msg->linear.y *= k;
          RCLCPP_INFO_THROTTLE(get_logger(), *get_clock(), 2000,
                               "clearance %.2f m - limiting to %.2f m/s",
                               clear, allowed);
        }
      }
    }
    pub_->publish(*msg);
  }

  static constexpr double RANGE_VALID = 0.05;
  double stop_dist_, clear_gain_, sector_half_, scan_fresh_;
  bool enabled_ = true;
  rclcpp::Publisher<Twist>::SharedPtr pub_;
  rclcpp::Publisher<Bool>::SharedPtr pub_state_, pub_beep_;
  rclcpp::Subscription<Twist>::SharedPtr sub_cmd_;
  rclcpp::Subscription<LaserScan>::SharedPtr sub_scan_;
  rclcpp::Subscription<Empty>::SharedPtr sub_toggle_;
  LaserScan::SharedPtr scan_;
  rclcpp::Time scan_t_{0, 0, RCL_ROS_TIME};
  std::vector<std::pair<bool, int>> pattern_;
  size_t step_ = 0;
  rclcpp::TimerBase::SharedPtr beep_timer_;
};

int main(int argc, char **argv) {
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<SafetyStop>());
  rclcpp::shutdown();
  return 0;
}
