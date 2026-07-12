# 接入与启动配置 / Setup & Access

> 从 README 拆出的"冷启动/接入/网络/远程桌面/相机预览/遥控"操作手册。
> 实验内容请回 [README](../README.md)。

---

## 0. 快速上手（冷启动）/ Getting started from scratch

> 刚拿到板子、换了台新电脑、或不确定板子现在什么状态，从这里开始。
> New machine / no context? Start here.

### 0.1 你的电脑先装好 / Host prerequisites

| 用途                | 工具                                               | 备注                         |
| ------------------- | -------------------------------------------------- | ---------------------------- |
| SSH 登录            | `ssh`（自带）；`sshpass`（可选，免交互输密码） | `sudo apt install sshpass` |
| 远程桌面            | 任意 VNC viewer（Remmina / RealVNC / TigerVNC）    | 连 §5 的`:5900`           |
| 相机 / Jupyter 预览 | 浏览器                                             | 直连板子 IP，见 §1          |

### 0.2 板子现在是哪种模式？先判断 / Which mode is the board in?

板子只有两种上网姿态，**先搞清楚现在是哪种**，否则连不上：

1. **客户端模式（当前默认）**：板子作为 WiFi 客户端接入家里路由器 `YOUR_WIFI_SSID_5G`。
   - 前提：**你的电脑必须连在同一个路由器/局域网**（`192.168.3.x` 网段），否则 `192.168.3.187` 根本 ping 不通。
   - 连接：`ssh root@192.168.3.187`（密码 `yahboom`）。
2. **AP 热点模式（出厂/异地兜底）**：板子自己发 WiFi 热点。**换环境、不在家里网络时用这个**。
   - 你的电脑连 WiFi `RDK_X5_Robot`（密码 `12345678`）。
   - 连接：`ssh root@192.168.8.88`（密码 `yahboom`）。
   - 想让板子切回客户端模式接新路由器，见 §4 `wifi_client.sh`。

### 0.3 连不上 / IP 找不到怎么办 / Can't reach the board

客户端模式下 IP 由路由器 DHCP 分配（**会变**，除非像本机一样在路由器后台做了静态绑定）。板子失联时：

```bash
# 方法 A：路由器后台找这台板子的租约（板子网卡 MAC = 18:ce:df:79:2e:8b）
# 方法 B：在与板子同网段的电脑上扫 ARP
ping -b 192.168.3.255 -c 3 2>/dev/null; ip neigh | grep -i '18:ce:df:79:2e:8b'
# 方法 C：彻底失联 → 断电重启板子回到能连的模式；或用 HDMI+键鼠直接上桌面看 IP
```

> ⚠️ 板载 OLED 屏只显示时间/内存等状态，**不显示 IP**，别指望它给地址。

### 0.4 登录后环境已就绪 / Environment is pre-sourced

板子的 `~/.bashrc`（root 和 sunrise 都）已自动 `source` 好 tros + 两个工作空间，并设 `ROS_DOMAIN_ID=99`。
**SSH 上去直接就能敲 `ros2 ...`，不用手动 source。** 若要在你的电脑上跑 ROS2 与板子通信，域号也要设成 `99`。

### 0.5 重刷机后一键恢复 / Restore after reflash

仓库 `board/` 目录按板子真实路径 1:1 镜像所有部署文件（脚本 + 自启服务）。刷完新系统后在电脑上跑：

```bash
scripts/deploy_board.sh [板子IP]     # rsync board/ → 板子 / ，并 enable 自启服务
```

---

## 1. 板子接入信息 / Access

> **当前状态（2026-07）**：板子已切到 **WiFi 客户端模式**，连家里路由器（5G），
> 路由器绑定了**静态 IP `192.168.3.187`**。日常用 `ssh root@192.168.3.187`（密码 `yahboom`）。
> 下面的 `192.168.8.88` 是 **AP 热点模式**下的地址，跑 `wifi_ap.sh` 切回热点时才用。

| 项目                             | 值                                               |
| -------------------------------- | ------------------------------------------------ |
| **当前 IP（client 模式）** | `192.168.3.187`（路由器静态绑定，5G）          |
| **AP 模式 IP**             | `192.168.8.88`（跑 `wifi_ap.sh` 切回热点时） |
| **SSH 用户 / 密码**        | `root` / `yahboom`                           |
| **自带热点 SSID / 密码**   | `RDK_X5_Robot` / `12345678`                  |
| 板载普通用户                     | `sunrise`（Yahboom 机器人代码在此账户下）      |

```bash
# 当前（client 模式）
ssh root@192.168.3.187         # 密码 yahboom
```

其他常用入口（当前 IP）：

| 服务          | 地址                          | 说明                                          |
| ------------- | ----------------------------- | --------------------------------------------- |
| JupyterLab    | `http://192.168.3.187:8888` | 官方交互式教程 / 例程                         |
| VNC (x11vnc)  | `192.168.3.187:5900`        | 远程桌面，**密码 `123456`**（已配置） |
| 相机 Web 预览 | `http://192.168.3.187:8000` | tros websocket（需先接好相机，见 §6）        |

---

## 2. 软件栈 / Software

- **OS**：Ubuntu 22.04.5 LTS，内核 **6.1.83** aarch64，RDK OS **3.0.0**（SD 卡 v1.0.0 / 20241206）
- **ROS**：ROS2 **Humble** + **tros-humble 2.3.0**（地平线 TogetheROS）
- **机器人工作空间**：`/home/sunrise/yahboomcar_ws`（建图/导航/视觉/巡线/多机等 20+ 功能包）
- **雷达驱动**：`/home/sunrise/software/library_ws`（`oradar_lidar`）
- **底盘 SDK**：`/home/sunrise/sunriseRobot`

---

## 3. 在线资料 / Vendor docs

- 官方教程：[https://www.yahboom.com/study/RDK-X5-ROBOT
  ](https://www.yahboom.com/study/RDK-X5-ROBOT)

---

## 4. WiFi 模式切换脚本 / WiFi mode scripts

板子出厂是 **AP 热点模式**（发射 `RDK_X5_Robot`，`hostapd` + `dhcpd`，自身 IP 192.168.8.88）。
以下脚本已部署到板子 `/home/sunrise/scripts/`（仓库 `board/home/sunrise/scripts/` 内留有同版本副本，按板子真实路径镜像）：

| 脚本               | 作用                                                                                                                                                                                  |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `wifi_client.sh` | 切到**客户端模式**，默认连接路由器 `YOUR_WIFI_SSID_5G`。家庭 WiFi 密码不写入仓库，运行时交互输入。内置国家码修复+扫描重试，连接失败会**自动回滚到 AP 模式**，防止锁死。 |
| `wifi_ap.sh`     | **恢复 AP 热点模式**（`RDK_X5_Robot` / `12345678` / 192.168.8.88）。                                                                                                        |
| `wifi_diag.sh`   | 诊断：底层`iw scan`（绕过 NM）列出可见 AP、标出 5GHz 与目标 SSID，跑完自动恢复 AP。                                                                                                 |

### ⚠️ 踩坑记录：连 5G 路由器扫不到 / Connecting to a 5 GHz router

**根因**：板载 WiFi 芯片 **AIC8800D80** 的国家码是驱动**模块参数**，出厂 `country_code=00`（world）
且 `custregd=Y`（自定义管制域，**忽略内核 `iw reg set`**）。world 模式下 5GHz 频段受限，
**扫不到任何 5G AP**。经宿主机实测，`YOUR_WIFI_SSID_5G` 是纯 5GHz（信道 44 / 5220 MHz），
所以板子一直扫不到、连接失败回滚。

**修法**（已内置进 `wifi_client.sh`）：写 `/etc/modprobe.d/aic8800.conf`
设 `options aic8800_fdrv country_code=CN custregd=N`，并重载 `aic8800_fdrv` 驱动使其生效。
另外，厂商 AP 镜像默认 mask 了 `wpa_supplicant.service`；切换客户端模式时必须先解除
mask 并启动该服务，否则 NetworkManager 会把 `wlan0` 标记为 `unavailable`，所有扫描都为空。
厂商 XFCE 自启动项 `Open_AP.desktop` 还会在每次登录时再次开启热点；client 脚本会禁用它，
AP 脚本则会恢复它，从而让所选模式在重启后保持一致。
2.4GHz 路由器不受国家码问题影响，但同样需要可用的 `wpa_supplicant`。

```bash
# 切客户端（运行后静默提示输入密码）
sudo /home/sunrise/scripts/wifi_client.sh

# 也可临时指定其他网络；注意命令行明文密码可能进入 shell 历史
sudo /home/sunrise/scripts/wifi_client.sh "SSID" "PASS"

# 恢复出厂 AP 热点
sudo /home/sunrise/scripts/wifi_ap.sh
```

> ⚠️ **切客户端会断开当前 AP 连接**：板子离开自己的热点、加入目标路由器后，
> 通过 `192.168.8.88` 的 SSH 会断。请到路由器后台看新 IP（板子 MAC `18:ce:df:79:2e:8b`）再重连；
> 想恢复热点，请在仍可访问板子时运行 `wifi_ap.sh`；恢复成功后，后续重启会继续保持 AP 模式。
>
> **回连热点的完整步骤**：① 电脑连 WiFi `RDK_X5_Robot`（密码 `12345678`）→ ② `ssh root@192.168.8.88`（密码 `yahboom`）。

完整实机指南：[`rdk-x5-wifi-client-guide.html`](rdk-x5-wifi-client-guide.html)。

---

## 5. VNC 远程桌面 / VNC

板子出厂自带 **x11vnc**（`x11vnc.service`，开机自启，共享 HDMI 的 XFCE 桌面）。已把密码设为 `123456`。

| 项     | 值                                                                            |
| ------ | ----------------------------------------------------------------------------- |
| 地址   | `192.168.3.187:5900`（display `:0`）                                      |
| 密码   | `123456`                                                                    |
| 改密码 | `x11vnc -storepasswd <新密码> /etc/.vnc/passwd && systemctl restart x11vnc` |

### 从零安装 VNC 服务端 / Install x11vnc from scratch

若镜像里没有 x11vnc（或想在别的 Ubuntu 机器上装一套同样的），三步：

```bash
# ① 安装
sudo apt update && sudo apt install -y x11vnc

# ② 设密码（存到 /etc/.vnc/passwd，与出厂路径一致）
sudo mkdir -p /etc/.vnc
sudo x11vnc -storepasswd 123456 /etc/.vnc/passwd

# ③ 写 systemd 服务并开机自启
sudo tee /etc/systemd/system/x11vnc.service >/dev/null <<'EOF'
[Unit]
Description=x11vnc remote desktop (shares :0)
After=display-manager.service
Wants=display-manager.service

[Service]
ExecStart=/usr/bin/x11vnc -display :0 -auth guess -rfbauth /etc/.vnc/passwd \
  -forever -shared -noxdamage -rfbport 5900
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now x11vnc
```

要点：

- `-display :0 -auth guess`：共享**已经在跑的 HDMI/XFCE 桌面**（不是另起虚拟桌面）；
  `-auth guess` 让它自己找 Xauthority，桌面必须已登录（板子出厂自动登录 sunrise）。
- `-forever -shared`：客户端断开不退出、允许多客户端同看。
- 需要虚拟桌面（无显示器、无自动登录）时改用 `tigervnc-standalone-server`，本板不需要。
- 卡顿优化 / 剪贴板失灵等问题，另见本机技能沉淀（x11vnc 参数调优：关桌面特效、`-noxdamage`）。

---

## 6. MIPI 相机预览 / Camera preview

**预览不用 VNC**，本机浏览器直连即可，三选一：`:8000`（tros websocket）/ `:80`（原厂 sunrise_camera）/ `:8888`（jupyter 例程）。
统一切换脚本 `camera_mode.sh` 支持 `tros`、`yahboom`、`hybrid`、`status`。`hybrid` 让 TogetheROS 独占 CSI0 提供视频，同时以 control-only 包装器运行亚博底盘和 TCP 6000 遥控。`camera_preview.sh` 是 tros 便捷入口，`camera_yahboom.sh` 是完整亚博 APP 便捷入口。

### 一键预览 / Quick start

```bash
ssh root@192.168.3.187                                   # 先登录板子
sudo /home/sunrise/scripts/camera_mode.sh tros      # 切到 tros 预览模式
# 然后你电脑浏览器打开：  http://192.168.3.187:8000
```

`camera_mode.sh` 用法：`sudo camera_mode.sh tros|yahboom|hybrid|status`
—— `tros` 给 web 预览，`yahboom` 交回亚博手机 APP，`hybrid` 视频给 tros + 保留底盘遥控，`status` 看当前谁在用相机。

> tros 和亚博 APP 对 CSI0 **互斥**——不能同时独占相机，所以切换前脚本会自动停掉另一方（`hybrid` 例外，见上）。

### 已验证：IMX219 位于 CSI0，能够出图

诊断实测（2026-07）：

- 相机是 **Sony IMX219**（读到芯片 ID `0x0219`，红灯亮=供电正常，I2C 通信正常）——**相机没坏、排线没坏、不需额外供电**
- 当前已在 **CSI0 / `i2c-6`** 扫到 IMX219 地址 `0x10`，官方 API 成功抓取 1920×1080 NV12 图像。
- tros `/image_raw` 实测约 **30 FPS**，浏览器预览监听 `http://192.168.3.187:8000`。
- `cam-service` 是 ISP/VSE 的 `/dev/isc` 后台中间件，必须保持 active；停止它会导致 `hbn_vnode_set_attr ... ret(-10)`。
- 亚博桌面自启的 `app_SunriseRobot.py` 会独占 CSI0，启动 tros 前必须先停止它（`camera_mode.sh tros` 已自动处理）。
- 相机画面实测偏暗；若现场并非暗环境，检查镜头是否被遮挡或保护膜是否仍在。
- 完整排障过程见 [`rdk-x5-mipi-camera-preview-guide.html`](rdk-x5-mipi-camera-preview-guide.html)。

---

## 7. 手动遥控与雷达 / Teleop & LiDAR basics

> 以下命令都在**板子上**跑（SSH 登录后环境已 source 好，见 §0.4）。可执行入口经实机 `ros2 pkg executables` 核实。
> 开机自启的完整导航/跟随栈见 README——日常不需要手动跑这些。

### 7.1 键盘遥控 / Teleop

开**两个** SSH 终端：

```bash
# 终端 A：启动麦克纳姆轮底盘驱动（订阅 /cmd_vel 驱动电机）
ros2 run yahboomcar_bringup Mcnamu_driver

# 终端 B：键盘遥控（按键发布 /cmd_vel）
ros2 run yahboomcar_ctrl yahboom_keyboard      # 手柄则用 yahboom_joy
```

> ⚠️ **安全**：一敲方向键车就真的会动。先把小车架空或留足空间，避免冲下桌。
> 手柄遥控：按 SELECT/BACK 使能（亚博使能锁每次开机复位），推杆即走。
> 底盘另有标定/巡逻入口：`yahboomcar_bringup` 下还有 `calibrate_linear` / `calibrate_angular` / `patrol`。

### 7.2 雷达 / LiDAR（2026-07-11 实机验证 ✅）

实测结论 / Verified: `/scan` 稳定 **10 Hz**，360°，量程 0.15–20 m，frame `lidar_link`。

```bash
# 只发布 /scan 数据（无图形界面，SSH 即可）
ros2 launch oradar_lidar ms200_scan.launch.py

# 雷达 + gmapping 建图
ros2 launch oradar_lidar ms200_scan_gmapping.launch.py
```

> ❌ **rviz2 在板端桌面必崩**：`ms200_scan_view.launch.py` 里的 rviz2 在板子 X 上稳定
> SEGV（Ogre/GL 栈问题；root/sunrise、`LIBGL_ALWAYS_SOFTWARE=1`、`QT_X11_NO_MITSHM=1`
> 全试过，约 6 秒必崩）。**看画面走浏览器方案或本仓库 GUI**，别浪费时间在 VNC + rviz2 上。

雷达设备节点 `/dev/oradar`；底盘 MCU 串口 `/dev/myserial`（115200）。

### 7.3 通用坑位 / General pitfalls

- **systemd-run 必须给 `HOME`**（或 `ROS_LOG_DIR`），否则 rcl 日志初始化 abort：
  `failed to configure logging: Failed to get logging directory`。
- **SSH 里 `pkill -f ms200_scan` 会杀掉自己的远程 shell**（命令行自匹配 → exit 255 无输出），
  用正则括号避开：`pkill -f "ms200_[s]can"`。
- 后台起 launch 别用裸 `&`（挂住 SSH stdout），用 `systemd-run --collect` 最干净。
- `ros2 topic pub --once` 会在 DDS 发现完成前发完退出（消息丢失），要加 `-w 1` 等订阅者。
