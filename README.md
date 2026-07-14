# RDK X5 Robot 实验记录 / Experiments

亚博（Yahboom）**RDK X5 ROBOT** 麦克纳姆轮小车上的机器人实验：自主导航、
相机+雷达融合跟随、自研桌面控制台。主控为地平线 D-Robotics **RDK X5**
（8×Cortex-A55 + 10TOPS BPU），预装 Ubuntu 22.04 + ROS2 Humble + TogetheROS（tros）。

> 传感器布局：顶部 ORADAR MS200 激光雷达（360° / 10 Hz）；车头 IMX219 MIPI 相机（斜向上装，
> 看人脸和上身）；四轮麦克纳姆轮（全向移动）。

![RDK X5 Robot](docs/images/car.jpg)

**接入 / 冷启动 / WiFi / VNC / 相机预览**等环境配置已拆到
👉 [`docs/setup.md`](docs/setup.md)。日常 `ssh root@192.168.3.187`，开机自启全套服务，直接做实验。

---

## 实验一览 / Experiments at a glance

| 实验 | 状态 | 入口 / 文档 |
| --- | --- | --- |
| 系统体检与硬件拓扑 | ✅ | [`docs/rdk-x5-system-report.html`](docs/rdk-x5-system-report.html) |
| 雷达/相机/底盘打通 + 浏览器仪表盘 | ✅ | [`docs/lidar-live-viewer.html`](docs/lidar-live-viewer.html) |
| Cartographer 建图 + Nav2 自主导航 | ✅ | §2，参数 `board/home/sunrise/nav_config/` |
| GUI 一键建图/存图工作流 | ✅ 实测 | §2，[`docs/rdk-x5-mapping-workflow.html`](docs/rdk-x5-mapping-workflow.html) |
| Episode 数据录制（rosbag2）+ 数据 Tab | ✅ 实测 | §4，[`docs/rdk-x5-dataset-recorder-design.html`](docs/rdk-x5-dataset-recorder-design.html) |
| cmd_vel 安全仲裁 + 驱动崩溃自愈 | ✅ | §2.2 事故复盘 |
| 激光急刹 safety_stop（rclcpp） | ✅ 实测 | §2.3，[`board/home/sunrise/ros2_ws/src/safety_stop/`](board/home/sunrise/ros2_ws/src/safety_stop/) |
| **Follow-me 相机+雷达融合跟随** | ✅ 实测可用 | §3，[`docs/rdk-x5-follow-me-fusion.html`](docs/rdk-x5-follow-me-fusion.html) |
| 桌面控制台 GUI（Tauri） | ✅ | §1，[`docs/rdk-x5-gui-architecture.html`](docs/rdk-x5-gui-architecture.html) |
| 官方实验取舍与进阶路线（4090 端云推理） | 📝 规划 | [`docs/rdk-x5-official-experiments-and-advanced-practice.html`](docs/rdk-x5-official-experiments-and-advanced-practice.html) |

---

## 1. 桌面控制台 GUI（Tauri 自研）

`gui/` 下的跨平台桌面应用（Tauri + rosbridge websocket），把散落的浏览器页面收敛成一个控制台，
四个 Tab：**仪表盘**（地图/雷达/相机三层叠加、点击图钉导航、建图/存图、急刹与跟随开关、全停）、
**📼 数据**（episode 列表/录制/预览回看/RViz 回放/拉取删除）、**系统**（硬件拓扑 + 软件栈框图）、
**日志**（板上 journald 流式查看）。状态栏：电量/温度/CPU/内存/硬盘 + 各话题活性灯。

**仪表盘**：地图（AMCL 定位，绿色箭头为车）+ 雷达点云（粉色）+ 相机画中画（含手势检测框叠加）。
**点击地图任意点 = 📍 图钉落地 + 下发导航目标**（再点=换目标，🛑 全停清除）。
顶栏 `🧍 跟随` 滑块开关直接 enable/disable 板上 `follow-me.service`，重启小车依然生效：

![GUI 仪表盘](docs/images/gui-dashboard.jpg)

**系统 Tab**：实机核实的硬件拓扑 + ROS2 软件栈框图（节点按出身着色：上游/tros/亚博/自研），
右侧汇总 systemd 自启服务与程序/配置落盘位置——新人看这一屏就知道整车软件怎么组织：

![GUI 系统拓扑](docs/images/gui-system.jpg)

架构细节（进程模型、rosbridge 订阅清单、离线降级）见
[`docs/rdk-x5-gui-architecture.html`](docs/rdk-x5-gui-architecture.html)。

---

## 2. 建图与 Nav2 自主导航（2026-07-11 实机验证 ✅）

- **建图（GUI 一键工作流，2026-07-14 ✅）**：仪表盘 `🗺 建图` 按钮启停
  `mapping.service`（仅 cartographer 本体，`Conflicts=nav2.service` 与导航互斥交接）；
  手柄慢速走一圈，画布实时显示地图生长，**暗橙=存图会被三值化丢掉的弱墙**（置信度 25–65），
  开近补扫变亮橙再存；`💾 存图` 跑 `map_save.sh`：备份 `room.bak.*` → 覆盖
  `room.{yaml,pgm}` → 自动切回导航。详见
  [`docs/rdk-x5-mapping-workflow.html`](docs/rdk-x5-mapping-workflow.html)。
- **导航**：`navigation_dwb_launch.py`（Nav2 + AMCL + DWB），调优参数
  `/home/sunrise/maps/nav_params_tuned.yaml`：`robot_radius 0.1→0.13`、膨胀半径
  `0.12/0.2→0.35`（否则贴着桌腿擦过去必撞）、限速 0.18 调通后提至 `0.6`。
- **交互**：GUI 里**点击地图任意点**即下发目标（📍 图钉常驻显示最新目标，
  终点朝向自动取车→目标方向），`🛑 全停` 取消。

### 2.1 开机自启服务（板上 6 个）

unit 文件在仓库 [`board/etc/systemd/system/`](board/etc/systemd/system/)，重启实测通过：

| 服务 | 作用 |
|---|---|
| `ms200-lidar` | 雷达驱动，发布 `/scan` |
| `rosbridge` | DDS → WebSocket 桥（`ws://<板子IP>:9090`） |
| `mipi-cam` | 相机 `/image_jpeg`（等 ISP 就绪再起，防开机竞态） |
| `nav-bringup` | 底盘驱动 + 里程计/EKF + 手柄 + cmd_vel 仲裁 mux + episode 录制器（§4） |
| `nav2` | AMCL + 规划器 + 控制器（自动喂初始位姿；无目标不动车） |
| `follow-me` | BPU 感知 ×3 + 融合跟随节点（GUI 开关控制，见 §3） |

另有按需单元 `mapping`（不自启）：cartographer 建图，`Conflicts=nav2` 互斥，GUI `🗺 建图` 启停。

```bash
# 换地图/重喂定位：sudo bash /home/sunrise/nav_config/nav_start.sh [map.yaml]
# 重刷机后恢复：在电脑上跑 scripts/deploy_board.sh（rsync board/ 镜像 + enable 服务）
```

### 2.2 cmd_vel 安全仲裁与事故复盘（重要）

- **仲裁 mux**（[`board/home/sunrise/nav_config/cmd_vel_mux.py`](board/home/sunrise/nav_config/cmd_vel_mux.py)，自研）：
  三优先级 `/cmd_vel_joy`(手柄) > `/cmd_vel_follow`(跟随) > `/cmd_vel`(Nav2) 汇入 → `/cmd_vel_mux`
  → §2.3 激光急刹 → `/cmd_vel_drv` → 驱动。
  导航/跟随中动手柄立即接管，松手 0.5 s 恢复；**空闲时持续发零速**（10 Hz）；
  另带**方向感知雷达护栏**：沿运动方向 ±30° 扇区取最近障碍，按余量线性限速，无有效数据视为堵死。
- **事故复盘**：亚博 `Mcnamu_driver` 的 RGB 灯 I2C 写入无异常保护，按手柄键可致
  **驱动进程崩死 → MCU 持续执行最后一条非零速度 → ROS 层任何停车手段全部失效**。
  修复=驱动 `respawn=True` + mux 空闲零速流，实测行驶中 `kill -9` 驱动 ≈1.5 s 内自动刹停。
  浏览器/GUI 的"终止"按钮**不是急停**——WiFi 断了它就是块砖；真急停=手柄使能键/拎车/电源开关。
- **手机 APP 与导航互斥**：亚博 APP 直写底盘串口（不走 ROS），与驱动并存会双写抢串口导致
  车抽搐。其 XFCE 自启已禁（`Start APP Program.desktop` 置 `Hidden=true`，改回即恢复原厂）。
- **systemd 坑**：厂商 `cam-service` 单元 `After=multi-user.target` 又 `WantedBy=multi-user.target`，
  任何 `After=cam-service` 的服务都会构成依赖环 → **开机静默丢弃 job（无日志）**。
- **电量**：`/voltage`（2S 18650，满 8.4 V；≈7.6 V 扩展板蜂鸣报警+限电机）。GUI 顶栏显示百分比。

### 2.3 激光急刹 safety_stop / Lidar safety brake（rclcpp，实测 ✅）

自研 C++ 节点 [`board/home/sunrise/ros2_ws/src/safety_stop/`](board/home/sunrise/ros2_ws/src/safety_stop/)，
串在仲裁 mux 与驱动之间（`/cmd_vel_mux` → safety_stop → `/cmd_vel_drv`），
过滤**最终输出**——手柄、跟随、Nav2 一视同仁：

- **净空比例限速，不是固定阈值急停**：沿运动方向 ±30° 雷达扇区取最近障碍，
  允许速度 = (净空 − 0.30 m) / 0.5，0.30 m 处归零。快=提前从 ~0.8 m 外开始压速度，
  慢=几乎无感。实测固定 30 cm 阈值在 1.0 m/s 全速下刹不住（10 Hz 雷达延迟+惯性滑行），
  比例限速全速冲墙可停。
- **方向感知,永不锁死**：前进查前方、倒车查后方、横移查侧方（MS200 360°）；
  背离障碍物的方向永远放行,被拦住后直接反向开走。原地旋转不拦。
- **fail-open**：雷达挂了放行不拦（手柄不能陪葬），日志告警；
  无人监督的跟随通道在 mux 层另有 fail-close 护栏兜底。
- **运行时开关**：开机默认开；GUI 顶栏 `🛡 急刹` 拨钮或手柄按键发 `/safety_toggle` 翻转，
  状态经 latched `/safety_enabled` 广播（节点持有状态,各处只发翻转+镜像显示,单一机制）。
  切换蜂鸣反馈：**开=滴滴两短,关=长滴一声**（走驱动 `/Buzzer` 话题）。
- 板上 `~/ros2_ws` colcon 编译；由 `nav-bringup` 启动、respawn 自愈；阈值/增益是 ROS 参数。

---

## 3. Follow-me：相机 + 雷达融合跟随（实测可用 ✅）

自研 rclpy 节点 [`board/home/sunrise/follow/follow_me.py`](board/home/sunrise/follow/follow_me.py)，
对着相机 **👌 OK 手势**锁定主人开始跟随（滴滴短鸣），**✋ 手掌**停止（长鸣一声）。
核心思路：**两条观测通道常开并行，不做模式切换**——

| 通道 | 传感器 | 提供什么 | 频率 |
| --- | --- | --- | --- |
| 身份通道 | 相机 + BPU（mono2d 人体检测 / 手势识别） | 是谁（track_id）、方位角、轮廓尺度 | ~30 FPS |
| 几何通道 | MS200 雷达腿聚类 | 精确距离与方位（360°，含车后） | 10 Hz |

单一控制环 10–40 Hz 融合两者：相机新鲜时用相机方位角，暗了（转身/出视野/1 m 内仰角丢人）
无缝落到雷达腿跟踪；雷达门限用相机方位锚定或按腿速度外推。关键设计：

- **认腿=运动判别**：桌腿/凳腿尺寸上与人腿不可分，唯一可靠特征是"会动"。
  用 `/odom` 把雷达聚类换算到**世界系**抵消自身运动——新咬合要求该位置 1.2 s 前是空的，
  持有中若 3 s 世界系静止且相机也黑，判定跟错了家具，放弃。
- **麦轮矢量速度**：速度向量 `vx=v·cosθ, vy=v·sinθ` 直指主人（不等车头转过去），
  同时 PD 转向（比例 + 方位变化率前馈）高频把车头甩向主人；直行 0.5 m/s，
  角度越大越加速，斜向最高 0.8 m/s。
- **丢失恢复**：短暂丢失（<10 s）主人回到视野即自动"滴滴"重锁；两通道全黑 2.5 s 才停车。
- **安全**：跟随速度同样过 §2.2 的 mux 雷达护栏与手柄抢占；感知超时 3 s 强制刹停。

启动链 [`follow_start.sh`](board/home/sunrise/follow/follow_start.sh) 拉起 3 个 tros BPU 感知节点
（人体检测→手部关键点→手势分类，全程 BPU 推理，单帧 ~10 ms）+ 融合节点，由
`follow-me.service` 管理，GUI 滑块一键启停并持久化。

算法细节（门限参数、状态机、手势投票、踩坑六条）见
👉 [`docs/rdk-x5-follow-me-fusion.html`](docs/rdk-x5-follow-me-fusion.html)。

---

## 4. Episode 数据录制与回放（rosbag2，实测 ✅）

为后续模仿学习/数据集积累做的一键示教录制：手柄 **START 键**或 GUI/数据 Tab 的 `⏺ 录制`
按钮开停，板端常驻节点 [`episode_recorder.py`](board/home/sunrise/nav_config/episode_recorder.py)
（随 `nav-bringup` 启动）把 11 个话题（scan/odom/tf/各级 cmd_vel/joy/相机 JPEG）录成
`~/episodes/ep_<时间戳>/`，蜂鸣反馈、磁盘余量护栏、时长上限（默认 3 min，GUI 可调）。
复用急刹开关同款**单一属主模式**：节点持有状态，手柄/GUI 只发 `/record_toggle` 翻转 +
镜像 latched `/recording`；🛑 全停走幂等 `/record_stop`（不会反向开录）。

**GUI 📼 数据 Tab**：episode 列表（倒序/大小/时长/板上余量）、行内展开快速回看
（帧滑条 + odom 轨迹 + cmd_vel 时间轴，走 ≤150 帧抽样 preview，5 MB 级秒拉）、
`🔭 回放` 一键拉包并本机 RViz2 复现（`scripts/replay.sh`，隔离 DOMAIN + sim time）、
拉取/删除。深度分析用 [`notebooks/episode_lab.ipynb`](notebooks/episode_lab.ipynb)。
设计与评审细节见 [`docs/rdk-x5-dataset-recorder-design.html`](docs/rdk-x5-dataset-recorder-design.html)。

---

## 5. 硬件速览 / Hardware at a glance

| 部件 | 规格 |
| --- | --- |
| 主控 | 地平线 **RDK X5 V1.0**（8×Cortex-A55 @1.5 GHz + BPU Bayes-e 10TOPS，6.5 GiB LPDDR4） |
| 相机 | Sony **IMX219** MIPI CSI0，1080P @30FPS，斜向上安装 |
| 激光雷达 | **ORADAR MS200** 360° 10 Hz，0.15–20 m（`/dev/oradar`） |
| 底盘 | 麦克纳姆轮 ×4 + STM32 扩展板（串口 `/dev/myserial`，含 IMU/电池管理） |
| 其他 | AIC8800 WiFi/BT、OLED 状态屏、2.4G 手柄接收器、40-pin GPIO |

完整体检（存储/总线/内核模块/温度）见 [`docs/rdk-x5-system-report.html`](docs/rdk-x5-system-report.html)。

---

## 6. 目录结构 / Repo layout

```
RDK-experience/
├── README.md                       # 本文件：实验记录主线
├── CLAUDE.md / AGENTS.md           # 给 AI 协作工具的项目说明
├── .memory/                        # 跨工具持久记忆（协议 SKILL.md + 索引 + 事实）
├── board/                          # 板端文件 1:1 镜像（路径与板子一致，重刷机后一键恢复）
│   ├── etc/systemd/system/         #   服务 unit（自启 ×6 + 按需 mapping，§2.1）
│   ├── home/sunrise/nav_config/    #   导航/录制自定义件（bringup/mux/joy_teleop/episode_*/map_save）
│   ├── home/sunrise/follow/        #   Follow-me 融合跟随（follow_me.py + follow_start.sh）
│   ├── home/sunrise/ros2_ws/       #   rclcpp 自研包（safety_stop 激光急刹）
│   └── home/sunrise/scripts/       #   WiFi 切换 / 相机模式切换等板端脚本
├── gui/                            # 桌面控制台（Tauri + rosbridge，§1）
│   ├── src-tauri/                  #   Rust 后端（ssh 命令、服务开关、episode 管理、单实例）
│   └── ui/                         #   前端（仪表盘/数据/系统/日志四 Tab）
├── notebooks/                      # 工作站分析 notebook（episode_lab / strafe_test）
├── scripts/                        # 主机侧工具
│   ├── deploy_board.sh             #   重刷机一键恢复：rsync board/ → 板子 / + enable 服务
│   └── replay.sh / rviz.sh         #   episode 本机 RViz 回放（隔离 DOMAIN + sim time）
└── docs/
    ├── setup.md                    # 接入/冷启动/WiFi/VNC/相机预览（从本文件拆出）
    ├── images/                     # 实拍与截图
    ├── rdk-x5-follow-me-fusion.html           # Follow-me 融合算法详解
    ├── rdk-x5-mapping-workflow.html           # GUI 一键建图/存图工作流
    ├── rdk-x5-dataset-recorder-design.html    # Episode 录制系统设计
    ├── rdk-x5-gui-architecture.html           # GUI 架构
    ├── rdk-x5-system-report.html              # 系统体检报告
    ├── lidar-live-viewer.html                 # 浏览器实时仪表盘（GUI 的前身，仍可独立用）
    ├── rdk-x5-wifi-client-guide.html          # WiFi 客户端切换实机指南
    ├── rdk-x5-mipi-camera-preview-guide.html  # MIPI 相机预览排障指南
    └── rdk-x5-official-experiments-and-advanced-practice.html   # 进阶路线
```
