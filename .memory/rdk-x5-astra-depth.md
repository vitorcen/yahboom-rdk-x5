---
name: rdk-x5-astra-depth
description: Orbbec Astra Pro USB 深度相机接入 GUI 左上窗;相机健康、板子供电是唯一卡点;软件全就绪
metadata:
  type: project
---

RDK X5 的 GUI 深度预览最终方案(2026-07-15):**用 USB Orbbec Astra Pro**,不是之前调研的
GS130W CSI 双目(那套 `docs/rdk-x5-stereo-depth-design.html` 已作废——CSI 双目要占满仅有的两个 CSI 口、
还要 OS≥3.3.3 升级,USB 相机简单得多)。GUI **左窗=深度伪彩、右窗=彩色**,右窗仍用 `/image_jpeg` 契约。

**相机身份**:Astra Pro = 两个 USB 设备:彩色 UVC `2bc5:0501`(MJPEG,出 `/dev/video0`)+ 深度私有
`2bc5:0403`(OpenNI 私有协议,非 UVC,要 OpenNI2 才能读)。**只有直插板子才枚举**——经 USB hub 供电不足认不到。

**驱动现成**:板上 `astra_camera`(OpenNI2)已编译在 `/home/sunrise/software/library_ws/install/astra_camera/`
(亚博 library_ws,和雷达同工作区),`astra_pro.launch.xml` 同时起 OpenNI 深度 + UVC 彩色。不用自己编。

**三个真正的坑(逐一实测定位,2026-07-15 全部搞定,深度已在板上跑通 ~9fps)**:
1. **供电**:相机经无源 USB hub 供电不足枚举不到、直插板子才认(投射器 ~2.4W 贴 USB2 500mA 上限;
   板子有欠压史 [[rdk-x5-nav2-plan]])。独立供电 USB hub 能稳;**后来实测:车子本身直接供电(电量足)时
   直插板子也能正常预览,不一定非要独立 hub**。供电是前置条件,但不是深度出不来的主因。
2. **`astra_camera` ROS 驱动的激光/LDP 逻辑坏**(主因):经 ROS 驱动深度恒为 0——它开机关激光,`set_laser_enable`
   开了也被 LDP 秒熄,`set_ldp_enable` 又报 `Couldn't set LDP enable` 关不掉。**判据**:同一相机用
   **裸 OpenNI2** 读(绕过 ROS 驱动)深度稳定 30fps、无塌陷(PC 和板上都验过,~12–16k 有效像素)。
   → **解法:彻底弃用 astra_camera ROS 驱动,自己用 OpenNI2 直读深度**。
3. **USB2 深度 vs 彩色带宽互斥**(硬物理):裸 OpenNI 深度单独跑 30fps;一旦同时开 UVC 彩色流
   (卡在 960×544@30,cv2 改不了分辨率/帧率)→ 彩色 isochronous 抢占带宽,**深度塌到 1.5fps**。
   这台相机的 UVC 没有更小的模式,压不下去。**结论:这条 USB2 上"流畅深度"和"彩色"不可兼得**。

**其它硬知识**:`ros2 topic echo/hz` 收不到深度是 QoS/CLI 老坑,判决一律用 rclpy 探针;`np.unique` 逐帧算
会拖慢探针误报低 fps(纯计数才是真帧率);深度设备 `2bc5/0403` 裸 libusb 要 root/udev(板上 root 无碍,
本机 PC 要 sudo);仓库自带 x64/arm64 OpenNI2 redist(`source/.../openni2_redist/`,含 `liborbbec.so`)。

**最终实现(已部署、开机自启、板上实测 depth ~9fps)**:
- `board/home/sunrise/nav_config/astra_preview.py`:**裸 OpenNI2(primesense,pip 装)直读深度 320×240**
  → JET 伪彩(红近蓝远)→ `/camera/depth/color_jpeg`(10Hz 定时器发)。**不用 astra_camera 驱动、
  无激光/LDP 折腾**(OpenNI 开流自动亮投射器)。彩色 `enable_color` 参数**默认关**(开了深度就塌)。
- 开机自启由 `camera-preview.service` + `camera_autodetect.sh` 负责(插 USB Astra 才跑该 py,
  插双目则跑 `stereo_cam.py`)。依赖 `pip3 install primesense`。**独立的 `astra-cam.service` 已删**
  ——它在 USB 空插时死循环重启且与 camera-preview 抢角色;autodetect 已覆盖 Astra 路径。见 [[rdk-x5-stereo-camera]]。
  (旧的 `astra_preview_launch.py` 已删。)
- GUI:`floatbox.js` 抽 `makeFloatBox()` 公用;左窗 `#depthbox` 订 `/camera/depth/color_jpeg`+iDepth 灯+
  掉流看门狗;右窗 `#cambox` 仍订 `/image_jpeg`。改 JS 要 **reload Tauri** 生效。

**未了/待定**:①**右窗彩色**——默认关(USB2 带宽);要彩色就得接受深度变卡(`enable_color:=true`),
或换 USB3 深度相机,或右窗改放别的。待用户定。②GUI reload 后目视确认左窗伪彩深度。
③follow-me 原用 CSI `/image_raw`(随 mipi-cam 停用而断),接回要改喂彩色源+几何适配(独立立项)。

相关:[[rdk-x5-robot-status]] [[rdk-x5-follow-me]] [[rdk-x5-nav2-plan]]
