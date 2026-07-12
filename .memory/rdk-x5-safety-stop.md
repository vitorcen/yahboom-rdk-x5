---
name: rdk-x5-safety-stop
description: rclcpp 激光急刹:mux 与驱动间净空比例限速;固定阈值高速刹不住的教训;开关/蜂鸣约定
metadata:
  type: project
---

激光急刹 safety_stop(2026-07-12,rclcpp,板上 ~/ros2_ws colcon 编译,实测可用):

- **拓扑**:`mux → /cmd_vel_mux → safety_stop(C++) → /cmd_vel_drv → 驱动`,过滤最终输出,
  手柄/跟随/Nav2 一视同仁;由 nav-bringup 启动,respawn 自愈。
- **核心教训:固定阈值急停对变速场景必然失败**——30cm 阈值慢速(0.3 m/s)能停,
  全速(1.0 m/s)必撞(10Hz 雷达延迟+惯性滑行)。正解=消除"急停"特殊情况,改净空比例限速:
  允许速度=(运动方向±30°扇区净空−0.30m)/0.5,快则从 ~0.8m 外开始压速,慢则无感。
- **方向感知永不锁死**:朝哪走查哪个扇区(MS200 360°),背离障碍方向永远放行;wz 不拦。
- **fail-open**:雷达挂了放行(手柄可用性优先);无人监督的 follow 在 mux 层另有 fail-close。
- **开关设计**:节点持有状态(开机默认开),外部只发 /safety_toggle(Empty)翻转,
  latched /safety_enabled(Bool) 广播——GUI 拨钮和手柄 A 键都是"发翻转+镜像显示",单一机制。
- **蜂鸣约定**(走驱动 /Buzzer Bool,True=响 False=停,时长由发布方控):
  急刹开=两短滴,急刹关=一长滴,全停(GUI 🛑全停按钮/手柄 Y 键)=一长滴。
- 全停 stop-all 契约(GUI 与手柄 Y 同款):取消 Nav2 目标 + disable follow-me
  + /cmd_vel_joy 零速连发 ~2s 压住总线 + 长滴。

相关:[[rdk-x5-gamepad-teleop]] [[rdk-x5-nav2-plan]] [[rdk-x5-follow-me]]
