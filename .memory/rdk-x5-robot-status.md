---
name: rdk-x5-robot-status
description: Yahboom RDK X5 小车的接入、已验证外设与进阶实践路线
metadata: 
  node_type: memory
  type: project
  originSessionId: 40f63512-00a9-4c05-8887-87341df6fa74
---

亚博 RDK X5 ROBOT 小车（地平线 RDK X5 主控，Ubuntu 22.04 + ROS2 Humble + tros 2.3.0）。仓库 `/Users/david/work_ai/yahboom-rdk-x5`。远程用 `sshpass -e ssh`（把 `SSHPASS` 设为 root 口令，**口令不入库，向用户获取**）。

> ⚠️ 凭据（SSH/VNC/AP/家用 WiFi/本机 sudo 密码）**一律不写进本仓库**（`.memory/` 会随 git 提交）。需要时向用户索取或从本机安全存储读取。下面只记结构，不记明文。

**当前接入（会变，验证前先 ping / 查 arp `18:ce:df:79:2e:8b`）：**
- 板子已切 **WiFi 客户端模式**连家里 5G 路由器，路由器绑定静态 IP **`192.168.3.187`**。`ssh root@192.168.3.187`（口令见上）。
- AP 热点模式地址是 `192.168.8.88`（SSID `RDK_X5_Robot`），跑 `wifi_ap.sh` 切回时才用。
- VNC：`192.168.3.187:5900`（x11vnc 出厂自带，已配密码）。
- 本机(david) 部分操作需 sudo。家里路由器 5G SSID `YOUR_WIFI_SSID_5G`、2.4G `YOUR_WIFI_SSID_2G`（同一路由 192.168.3.x）。

**板子已部署脚本** `/home/sunrise/work/scripts/`（仓库 `scripts/` 有副本）：`wifi_client.sh` `wifi_ap.sh` `wifi_diag.sh` `camera_preview.sh`。

**WiFi 5G 坑**：aic8800D80 国家码是驱动模块参数，出厂 `country_code=00`+`custregd=Y`（忽略 `iw reg`），5G 扫不到。修法=写 `/etc/modprobe.d/aic8800.conf` 设 CN 并重载驱动。5G 最终是 codex 修好的。详见 README。

**相机进展（2026-07-10，已出图）**：相机=Sony IMX219，位于 **CSI0=i2c-6**，地址 `0x10`。官方 `libsrcampy` 已成功抓取 1920×1080 NV12 实际帧，tros `/image_raw` 与 `/image_jpeg` 均约 30 FPS，预览入口为板端 `:8000/TogetheROS/`。画面实测偏暗，但有纹理和明暗变化，不是空帧。
- 根因与正确依赖：① `cam-service` 是 ISP/VSE 所需的 `/dev/isc` 中间件，必须 active；停止它会触发 `hbn_vnode_set_attr ret(-10)`。② 亚博 `app_SunriseRobot.py` 会独占 CSI0，与 tros 预览互斥，启动预览前要停止它。③ 只看到 8000 端口不代表相机出图，必须用 `ros2 topic hz` 验证。
- 当前启动行为：tros 预览为手工后台启动，未配置 systemd 自启；重启后亚博 XFCE APP 会恢复自启。详见 `docs/rdk-x5-mipi-camera-preview-guide.html`。
- 模式切换：`camera_mode.sh` 支持 `tros`、`yahboom` 和 `hybrid`。hybrid 用 control-only 包装器保留亚博 TCP 6000 遥控，由 TogetheROS 独占 CSI0 并在 8000 提供视频。

用户偏好：中文沟通、直接犀利；每弄清一个问题就让我把结论记进 README 存档。

**进阶路线（2026-07-10）**：新增 `docs/rdk-x5-official-experiments-and-advanced-practice.html`，按 P0/P1/P2 整理官方实验，并给出 Nav2、速度安全监督、数据采集、BPU 感知、Wi-Fi ROS 2 远端推理与 LiDAR-to-Action 的渐进项目清单。
- RDK X5 负责传感器接入、底盘驱动和本地安全闭环；4090 负责训练与大模型推理。远端动作只能是 proposal，RDK 侧必须做 watchdog、scan 近场约束、限速和过期动作拒绝。
- 2D LiDAR 可直接输入学习策略并输出移动动作；加入图像与语言条件后才是 VLA 类导航。2D 扫描存在高度盲区，真机实验必须低速、封闭场地并保留人工接管。
- 当前系统为 RDK OS 3.0.0；较新的 Model Zoo 推荐 RDK OS >= 3.5.0。升级需先克隆 SD 卡并用第二张卡验证，不能在当前可用 Yahboom 系统上直接滚动升级。
