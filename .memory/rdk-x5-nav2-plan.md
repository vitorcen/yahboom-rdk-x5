---
name: rdk-x5-nav2-plan
description: Nav2 自动导航已跑通（2026-07-11）：建图/AMCL/DWB/浏览器拖线导航/手柄仲裁/看门狗；含驱动崩溃事故教训
metadata:
  node_type: memory
  type: project
---

**Nav2 自动导航已实机跑通（2026-07-11）**。链路：cartographer 建图存 `room.yaml` →
`navigation_dwb_launch.py`（AMCL+DWB，调优参数 `nav_params_tuned.yaml`：robot_radius 0.13、
膨胀 0.35、限速 0.18）→ 浏览器 viewer 拖线发 `/goal_pose` 车自动走。5 个 systemd 服务
开机自启（雷达/rosbridge/相机/底盘/nav2），重启实测全绿。详见 README §8.2–8.3。

**关键教训（花真时间踩出来的）**：
1. **亚博 Mcnamu_driver 会崩死在无保护的 RGB 灯 I2C 写入**（OSError 121）→ MCU 持续执行
   最后一条非零速度，ROS 层停不住车。修复=launch 里 `respawn=True` + 自研 `cmd_vel_mux.py`
   （手柄优先仲裁 + **空闲持续 10 Hz 零速流**）。行驶中 kill -9 驱动实测 ≈1.5 s 自动刹停。
2. **亚博 APP 直写底盘串口**（不走 ROS），与驱动并存=双写抢串口车抽搐；其 XFCE 自启
   `Start APP Program.desktop` 已置 `Hidden=true`（手机 APP 遥控原厂功能因此失效，二选一）。
3. **厂商 cam-service 单元自嵌 multi-user 环**：`After=multi-user.target`+`WantedBy=multi-user.target`，
   任何 `After=cam-service` 的自启服务会被 systemd **开机静默丢 job（无任何日志）**。
   mipi-cam 用 ExecStartPre 轮询 `systemctl is-active cam-service && test -e /dev/isc` 代替排序。
4. `ros2 topic pub --once` 在 DDS 发现完成前发完即退，消息丢失（AMCL 初始位姿就这么丢过）→
   必须 `-w 1` 等订阅者匹配。
5. 手柄"没反应"大概率是亚博 `yahboom_joy` 的使能锁：`Joy_active` 开机复位 False，
   按 SELECT/BACK（buttons[6]/[4]）切换后才发 cmd_vel。
6. 欠压先查 `/voltage`：2S 18650，≈7.6 V 扩展板持续蜂鸣+限电机，表现酷似"遥控坏了"。

下一步候选：遥控建全屋图（Task4）、TEB 换 DWB（麦轮横移）、collision_monitor、
手机网页摇杆发 `/cmd_vel_joy`（走同一 mux）。远期路线见
`docs/rdk-x5-official-experiments-and-advanced-practice.html`（本 mux 即 cmd_vel_guard 种子）。
相关：[[rdk-x5-robot-status]]
