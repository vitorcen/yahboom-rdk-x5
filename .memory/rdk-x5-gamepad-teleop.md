---
name: rdk-x5-gamepad-teleop
description: 手柄失灵根因(厂商 Joy_active 锁存)与无状态 joy_teleop 重写;实测键位表;校准教训
metadata:
  type: project
---

手柄遥控重写(2026-07-12,nav_config/joy_teleop.py 替换厂商 yahboom_joy):

- **根因**:厂商节点 root 下走 `user_sunrise` 分支,`Joy_active` 锁存默认 False 且
  每次重启归零(手柄静音);解锁后又在每条 /joy 上发 twist(joy_node 19Hz 自动重发)
  → 零速刷屏永久占住 mux 最高优先级,饿死 follow/Nav2。两态各坏一头。
- **修法=消除状态**:无锁存,杆量出死区即发布,松手发 3 帧零速后闭嘴,mux HOLD
  0.5s 后自动让权。joy_node/joy_teleop/cmd_vel_mux 全部 respawn=True。
- **实测键位**(0079:181c 杂牌接收器,全部板上长按抓包,勿信惯例):
  axes[1]/[0]=左杆前后/转向,axes[2]=右杆横移,axes[6]/[7]=方向键帽轴(左/上=+1,
  发半杆量≈0.5 天然慢速),buttons[3]=X 左横移,buttons[1]=B 右横移;
  **axes[4]/[5] 是静息恒 +1.0 的扳机轴,映射它=永久蠕动指令**(踩过)。
- **校准教训**:第一轮就要 axes+buttons 全量抓,只盯轴会把帽轴误认成按键轴
  (走了三轮弯路);校准用"长按 5 秒一次一键",短按混按解不开。
- GUI 终止按钮已升级:取消 Nav2 目标 + /cmd_vel_joy(最高优先级)零速连发
  + 顺手关 follow 开关(旧版零速发最低优先级 /cmd_vel,follow 一开口就无效)。

相关:[[rdk-x5-strafe-weak-rear]] [[rdk-x5-follow-me]] [[rdk-x5-nav2-plan]]
