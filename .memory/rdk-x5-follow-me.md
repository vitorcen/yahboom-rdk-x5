---
name: rdk-x5-follow-me
description: Follow-me 视觉跟随已实现并台架验证;mux 升级三优先级+雷达钳位;关键教训与待实测清单
metadata:
  type: project
---

Follow-me 视觉跟随(2026-07-12,方案经 codex gpt-5.6-sol 评审后收敛为 MVP,详见
docs/rdk-x5-follow-me-vision.html):

- 板上文件:`/home/sunrise/follow/{follow_me.py,follow_start.sh}`(repo 镜像 board/)。
  感知链复用常驻 mipi-cam 的 `/image_raw`(960×544 ros 模式),mono2d+hand_lmk+gesture
  三 BPU 节点全链 ~22FPS、推理 10ms。跟随节点只订合并话题 `/hobot_hand_gesture_detection`。
- 手势值(亚博源码 common.h 核实):Okay=11 锁定(3 帧、唯一候选)、Palm=5 任何人停止。
  停止全部锁存,重新 OK 才恢复。蜂鸣 /Buzzer 是 std_msgs/Bool;RGB 弃用(I2C 崩溃前科)。
- mux 升级(nav_config/cmd_vel_mux.py):`/cmd_vel_joy > /cmd_vel_follow > /cmd_vel` 三优先级;
  follow 源独立雷达钳位——scan 过期>0.4s 全零、前扇区±30°<0.35m 禁前进、永远禁倒车。
- **教训:MS200 在 ≤0.1m 盲区/吸光面返回无效回波(0.0),前扇区"无有效数据"曾被算成
  front_min=inf 当畅通放行(实测抓获)。无数据必须当有障碍。**
- 评审要点:跟错人比丢人危险(否决颜色重识别/SEARCH);框裁剪后 d=k/h 反向失效(贴上沿
  即判无效);SELECT 是软件仲裁非硬通道;声光必须异步。
- 待实测(需真人):OK/Palm 手势、跟随行为、故障注入、物理停车距离(见文档验收表 3-7)。
  lidar_link yaw=0,scan 角 0=车头。

相关:[[rdk-x5-robot-status]] [[rdk-x5-nav2-plan]]
