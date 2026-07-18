---
name: rdk-x5-stereo-camera
description: 小车相机从单目 IMX219 换成 GS130WI 双目全局快门(双 SC132GS),两 CSI 已电气核实;RDK/tros 支持 sc132gs;尚未 bring-up
metadata:
  type: project
---

2026-07-18:小车把 **CSI 相机从单目 IMX219 换成 GS130WI 双目立体相机**(用户口述,
已上板电气核实)。"GS130" = **G**lobal **S**hutter + 130 万像素。

**两 CSI 核实(i2cdetect + 读芯片 ID):**
- **CSI0 → i2c-6**:SC132GS @ `0x32`,chip ID = `0x0132`(reg 0x3107/0x3108 = 01 32)✓;
  同总线 `0x50` 是 **出厂标定 EEPROM**(头 5 字节 ASCII "UNION",后接 IEEE754 double
  数组 = 双目内参 fx/fy/cx/cy + 畸变 + 左右目旋转/平移基线)。0x58/0x68 也 ACK(次级 eeprom?)。
- **CSI1 → i2c-4**:SC132GS @ `0x30`,chip ID = `0x0132` ✓。
- vcon(vin connector)映射:`vcon@0/1→i2c-6`(CSI0)、`vcon@2/3→i2c-4`(CSI1);
  4 个 mipi RX port(mipi0-3)都注册,每口 2 lane。

**sensor = SC132GS**(思特威 SmartSens,1.3MP 全局快门 RAW10)。全局快门→机器人运动/
双目 VSLAM 无果冻效应,比 IMX219 卷帘更适合。

**RDK/tros 支持齐全**(2026-07-18 核实):
- sensor 库 `/usr/hobot/lib/sensor/libsc132gs.so`;tuning `/usr/hobot/bin/sc132gs_tuning.json`;
  官方样例 `/app/multimedia_samples/vp_sensors/sc132gs/`(两模式:`1088x1280 raw10 30fps 1lane`、
  `896x896 raw10 10fps 2lane`)。
- tros `mipi_cam` 插件字符串含 `sc132gs`,带 `vp_sensor_detect_2` 自动探测(扫 mipi rx csi +
  i2c 地址)。

**现状/坑:**
- **板上还没装亚博 GS130WI 软件**(grep 无 gs130 配置),但 RDK 栈自带 sc132gs 可自行 bring-up。
- 老 `mipi-cam.service` 是按 **IMX219 单目 + :8000 websocket** 配的(disabled+inactive,没崩;
  ExecStart 走 `mipi_cam_websocket.launch.py` 靠自动探测,理论能认 sc132gs 但只起单路)。
  **双目要新写 bring-up**(两路 CSI 同时起 / 立体标定 / 深度)。
- 那些 i2c `UU` 都不是相机:i2c-2 `0x1c`=hpu3501 电源管理,i2c-7 `0x18`=ES8326 音频 codec,
  i2c-7 `0x3b`=sii902x HDMI。相机 sensor 在 hobot 框架下不走 sysfs probe(userspace 库直读 i2c)。

**图像已实证(2026-07-18,双眼同时出图):**
- **右眼**开箱即用:`libsrcampy.Camera().open_cam(0,-1,30,1088,1280)` 自动检测到 0x30 那只,
  `get_img(2)` 出 NV12(1088×1280×1.5=2088960B)→ Encoder JPEG,实拍真图(偏品红=ISP 白平衡
  没匹配这颗 sensor,偏暗=AE 没settle)。**libsrcampy 只能自动开第一只,开不了两只**(显式
  video_index=0/2 报 "No camera sensor found")。
- **两眼同开**走 RDK 官方 `get_vin_data`(`/app/multimedia_samples/sample_vin/get_vin_data`,
  支持最多 4 路 `-s`)。我加了一份左眼配置 `vp_sensors/sc132gs/linear_..._1lane_left.c`
  (`sensor_i2c_addr_list={0x32}`、symbol `..._1lane_left`、name "sc132gs-left"),注册进
  `vp_sensors/vp_sensors.c`(extern+数组,现 **index 4=右眼0x30 / index 5=左眼0x32**),`make` 重编。
  `./get_vin_data -s 4 -s 5` 按 `g` 同时 dump 两路 RAW10(1088×1280,uint16,stride 2176,
  buffer 2785280B)。numpy 归一化后左右并排可见明显水平视差=真立体对。RAW 偏暗(max~194/1023)。

**GUI 集成 · 阶段1 彩色右窗(2026-07-18 完工并服务化):**
- `board/home/sunrise/nav_config/stereo_cam.py`:libsrcampy 单眼(自动开 0x30 右眼)→ NV12→BGR
  →**先缩到 544 宽再 gray-world 白平衡**(修 ISP 品红偏色,全分辨率算会掉到 5.5fps,缩后再算 **~9fps**)
  → JPEG → 发 `/image_jpeg`(sensor_msgs/CompressedImage)。板上实测自然色、曝光正常。
- **开机自动探测**`board/home/sunrise/nav_config/camera_autodetect.sh`:探 **i2c-6 0x50 EEPROM 前5字节
  ="UNION"**(=GS130WI,**MCLK 无关最可靠**;sensor chip-id 探测不行——libsrcampy 用过 0x30 后该地址
  i2c 掉线 NAK)→ 跑 stereo_cam.py;否则探 USB Astra `2bc5:0403`→ astra_preview.py;都没有→ sleep。
- `board/etc/systemd/system/camera-preview.service`:跑 wrapper,**取代 astra-cam.service**(已 stop+disable);
  依赖 cam-service+/dev/isc。已 enable 开机自启,板上实测 active、9fps。
- GUI 前端**无需改**:契约与 Astra 相同(`/image_jpeg`→右窗#cambox)。注:现喂的是**右眼 0x30**,
  而 GUI 文案写"双目模式=左目"——阶段2 上 HBN 后改喂左眼对齐。

**阶段2 深度左窗(2026-07-18 完工,端到端板上实测):**
- **UNION EEPROM 标定已解码**(0x50,1KB):头"UNION"+版本;cam1@0x18 / cam2@0x81 各 12 double
  (fx fy cx cy + k1 k2 p1 p2 + 4 零),0x78/0xE1=重投影误差(0.23/0.23px);R(9 double)+T@0xEA,
  **基线 69.69mm**,T 后跟 |T| 自校验。cam1 有效焦 666.5/666.7 中心(526.8,637.5)。
  **实证对应:标定 cam1↔addr 0x30、cam2↔0x32**(SGBM 有效率 41.5% vs 换边 23.1%)。
- **stereo_capture.c**(C daemon,`board/home/sunrise/nav_config/`,板上 gcc 现编):每眼一线程从
  ISP chn0 拉 NV12,原子写 `/dev/shm/stereo_cam{0,1}.nv12`(32B 头 STER+w/h/stride/fid/ts)。
  坑:①vin `hbn_vnode_open(HB_VIN, hw_id=cim_attr.mipi_rx)`——写死 0 第二路 attach 必挂;
  ②flyby(vin→isp online)不给 vin 配 ochn buffer(要 RAW 分接才配,STEREO_TAP=vin);
  ③照抄 get_isp_data 的 vin_attr_ex/mclk 处理;④kill 后立刻重开会 -10(资源释放竞态,等 2s 重试)。
- **左眼硬件故障排查实录**:左眼(当时 CSI0)ISP 后噪声 9-11(右 0.9)、掉帧 40%、AE 震荡;
  RAW 噪声却正常、settle 扫描无效、Bayer 相位一致、单路独占也脏 → **`mipi_host0 pkt_fatal` 持续
  累积(~7/s)而 host2 恒 0 = MIPI 物理层误码**。用户对调板端 CSI 排线后**全好**(pkt_fatal 双 0、
  噪声 2.2/2.4、满 30fps)——根因=**原 CSI0 排线接触不良,插拔即愈**。诊断利器:
  `/sys/class/vps/mipi_host*/status/icnt` 的 pkt_fatal 计数。
- **stereo_cam.py v2**:读 EEPROM(i2ctransfer, bus 4/6 都试,缓存 stereo_calib.bin)→ stereoRectify
  (0.4 尺度 432×512 16 对齐)→ SGBM(64 视差 blk5 3WAY,~257ms,最近 0.29m)→ JET(红近蓝远)发
  `/camera/depth/color_jpeg`;彩色=0x30 眼 NV12 **先分平面缩再 cvtColor**(省 4 倍)+ **WB 增益 30 帧
  缓存 convertScaleAbs**(67ms→6ms)发 `/image_jpeg`。**彩色/深度各自 python 线程**——SGBM 阻塞
  rclpy 单线程 executor 会把两路都拖到 1.8fps。spawn/watchdog stereo_capture 子进程。
- 帧配对:daemon WRITE_EVERY=1(每帧写),节点按头部 ts 配对(>60ms 弃)。注意 get_isp_data 样例
  不消费帧,dump 永远是 frame0(AE 未收敛)——判断 settled 行为必须用连续消费的 daemon。
- autodetect 的 EEPROM 探测**bus 4/6 都扫**(0x50 随排线走,对调后换总线)。
- 开机风暴(load 15+)下实测 color 8fps / depth 2.8fps;正常 load 待复测。GUI 契约不变,前端零改动。

**模组身份/高帧率探索(2026-07-18):**
- 模组=微雪 **RDK Stereo Camera GS130WI**(waveshare.com/rdk-stereo-camera-module-gs130w.htm):
  双 SC132GS + **板载 ICM-42688-P 六轴 IMU**(=i2c-6 一直 ACK 的 `0x68`!未接入)+ 同步双目曝光/外触发。
- **SC132GS 芯片 120fps 全局快门**;板上旧 libsc132gs.so 只有 30fps 表。上游源码
  **github.com/D-Robotics/x5-libcam-sensor** 有 `1088x1280_60fps_setting_master` + 30fps slave 表
  (master/slave=硬件同步);编译缺 SDK 私有头(hb_i2c.h/hb_cam_utility.h 板上无)。
- **触发架构真相**:sensor 运行于**外触发模式**(vts=0x3fff 拉满,PLL 与 60fps 表相同),
  **帧率=X5 LPWM 触发脉冲频率**(vp_sensors 配置 `lpwm_attr.period=33333us`=30fps)。
  两眼 LPWM 同源→**硬同步实测 ts_diff=0.0ms**(WRITE_EVERY=1 后)。
- **60fps 达成(2026-07-18,板上实测双眼 60.0fps 稳定)**。完整因果链:
  ①`-f` 覆写 lpwm period 生效(sysfs `lpwm_config_info` 见 16666us)但仍 30fps——触发模式单帧
  序列 >16.6ms,60Hz 触发掉一半;缩 vts 也救不了触发模式。
  ②钥匙=**`0x3222=0x00` 关触发模式**切自由跑:停流(0x0100=0)→时序窗口
  (0x3201/03=0x02,0x3205=0x55,0x3207=0x15,0x3213=0x0c)→**vts 0x320e/0f=0x0578(1400)**
  →0x3222=0→开流。PLL/MIPI 线速与 30fps 表相同,**流中热打即可**,pkt_fatal 不涨。
  ③已固化:`stereo_cam.py` 的 `bump_sensors_60()` 在 spawn daemon 6s 后自动对两眼(chip-id 探测
  bus4/6)打序列,watchdog 重生同样触发;服务重启/开机全自动。
  ④自由跑两眼失去 LPWM 硬同步,相位差实测 ~1.7ms(固定相位,配对无碍);要硬同步得研究
  master FSYNC→slave(上游 slave 表的 0x3222=0x02/0x3223 等)。
  ⑤2lane 896x896 模式 PHY 全静默(模组可能只布 1lane);1lane 60fps 带宽 0.96G<1.2G 够;
  120fps 需 2lane/更高线速,未做。
  ⑥8 核满载(nav2+全栈 load~15)时 GUI 发布 7.2/2.6fps 是 CPU 竞争,shm 源稳定 60fps
  可供录制/低延迟用途直读。

**阶段3 · BPU(NPU)深度上线(2026-07-18 深夜,GUI 实测 depth 5.4fps / color 6.2fps @全栈负载):**
- **官方 mipi_cam 双目驱动彻底绕开**:2.5.2 的 132gs launch 在本机 GDC 环节必挂
  (gdc bin 缺失/异型号 `./sc230ai_gdc.bin`),rotation=0 直接 abort;且升 `hobot-camera` 3.1.1 会与
  旧系统头 **ABI 断裂**(hbn_camera_attach_to_vin 必挂)——已回滚 3.0.1(教训:**tros 上层包与
  hobot-* 底层有配套矩阵,不能单升;升 hobot-camera 必须重编 stereo_capture**,备份
  /root/sensor.bak.3.0.1)。mipi-cam 2.5.2 / hobot_stereonet 2.5.5 保留(stereonet 不碰 sensor 层零冲突)。
- **自研喂料链**:stereo_cam.py 平面级 rectify(裁剪+等比缩放折进目标投影 P,免压扁——codex 评审
  修正)→ combine `[Y_L][Y_R][UV_L][UV_R]` 640×704 nv12 → `/image_combine_raw` → 官方
  DStereoV2.4_int8(BPU,实测 BPU 占用仅 11%)→ visual → **hobot_codec(C++ 硬件 JPEG)桥接**到
  `/camera/depth/color_jpeg`。深度数值实测正确(近0.2m/远9m 合理,基线 69.69mm 标定直用)。
- **三个硬坑(排障链 py-spy 定谳)**:①**rclpy `msg.data=bytes` 赋值走逐字节 python 慢路径,
  675KB≈700ms**——必须 `array.array('B').frombytes()` 直通 setter(百倍差);②rclpy 反序列化大图
  同样慢(1.35MB visual 占 GIL 800ms)——大图回程一律走 C++ hobot_codec,rclpy 永不订大图;
  ③配对轮询:两眼自由跑 sensor ts 差呈双值分布(相位±帧间),半帧门限(8.5ms)+3ms 轮询 6 次。
  另 UDP buffer 已提 16MB(/etc/sysctl.d/99-ros2-bigmsg.conf,当时嫌疑后排除,留着无害)。
- 实验残留双份 stereonet 会吃满 CPU——排障时注意 pgrep 清场。GUI 两窗左上角有绿色 fps 角标
  (发布端滑窗自算);深度窗另有 stereonet 官方 overlay(FPS/Latency/CPU/BPU)。
- `DEPTH_BACKEND=sgbm` 环境变量回退 CPU SGBM。待办:#13 已知距离精度验证。

**阶段B C++/零拷贝完成(2026-07-18,#12):GUI 深度 5.4→16.8fps,彩色 3.4→10.1fps(唯一载荷计数)。**
- 三层根因逐个击破:①python 单进程 color+combine 双线程**共抢 GIL 合计上限 ~11 it/s**——热路径
  全部下沉 C++ 节点 `stereo_combine_pkg`(colcon/ament,产物 `stereo_combine_node`,fixed-point
  CV_16SC2 remap 直写 msg 缓冲);②消费端轮询配对全量读 2MB 文件烧 tmpfs 带宽(144% CPU 才 6fps);
  ③致命前提崩塌:系统负载下**两眼各自独立丢帧 ~13%**,"半帧内近对必存在"死掉(实测 dt p50=12ms
  超 8.5ms 门限)→ **配对下沉到 stereo_capture 守护**(两眼数据都在它手里):每眼 staging,后到者
  查对方 ts,±8.5ms 内 seqlock 写单一 `/dev/shm/stereo_pair.shm`(64B 头 seq 奇偶锁 + 双 NV12,
  mmap 零 syscall,PAIR_HZ 默认 30)。消费端(C++/python fallback 统一)mmap 快照,零轮询零配对。
- `infer_thread_num:=4`(默认 2)推理流水线加深:BPU 24%→63%,吞吐显著抬升。CPU 已顶格 1.5GHz
  无超频档;stereonet 推理是当前瓶颈,官方 27fps 是裸推理(无 visual 渲染)口径,GUI 要看图不关。
- 测量陷阱:`ros2 topic hz` 订阅 1.35MB 大图时 python 反序列化跟不上会**漏计**(visual 假 8.3);
  stereonet 自绘 FPS 角标口径不明(报 4.7 时唯一 jpeg 实测 16.8)——**以唯一载荷 md5 计数为准**。
  另 python 闲置 publisher 会让 `ros2 topic info` 显示双发布者,别误判。
- 文件:`stereo_combine_pkg/`(package.xml+CMakeLists+src)、`build_stereo_combine.sh`(板上
  colcon,产物拷 `stereo_combine_node`,**Text file busy 需先 stop 服务**);stereo_cam.py 退居
  监督者(标定/导出 rect_maps.bin RMAP1 格式/拉起看护 5 进程),`COMBINE_BACKEND=py` 回退 python。
- **彩色终局=事件驱动+硬件 JENC**:软 imencode(~30ms/帧)是抢深度 CPU 的元凶(彩色放开 20 深度
  16.8→13.7)。改 NV12 直发第二个 hobot_codec 实例(`codec_channel:=2`,`/image_color_nv12`→
  `/image_jpeg`,fps 角标画在 Y 平面白字),`COLOR_HZ=0` 默认事件驱动跟配对率。终态:**彩色 19.8 /
  深度 ~14-17fps**。60fps 彩色被否:前处理 16ms/帧×60 吃满一核、深度再掉 3-5,GUI 渲染 30Hz 收益为零。

**彩色偏粉终审(2026-07-18,#14 结案):物理限制,非配置 bug。**
- 取证链:pre-ISP RAW 2×2 相位统计(`STEREO_TAP=vin`)证实**彩色 BGGR Bayer**(绿对角 64.7/64.7
  相等、蓝位 48.8、红位 60.7);tuning json(`calib_lname`)实际已加载(AWB PCA/CCM/DMSC 齐全)。
- 根因:**模组无 IR-cut 滤光片**(NIR 850/940nm 增强是卖点)。室内灯近红外灌爆 R/B → 品红加性
  污染+径向 shading;`logcat`(板上有)见 AWB 永远 "can not calculate right color temperature"
  (白点在自然光轨迹外)→ 色温=0 → CCM 不套用。**任何矩阵不可逆,真修复只有硬件加 650nm IR-cut**。
- 软件上限已落地:①`stereo_capture.c` `freeze_isp_wb()`(启动 3s 后锁手动 WB,优先级:标定表
  gain_by_temper(此模组表为空)→`ISP_WB_GAINS="r,b"`→冻结 auto 当前值;`ISP_WB_TEMPER=0` 关);
  ②`stereo_cam.py` `chroma_fix()`:半分辨率 UV 按径向环带(24 bins,np.bincount)扣色度基底
  +1.6× 饱和,EMA 0.3 防闪。局部真实色存活,大面积表面必然被中和(均值扣除数学性质)。
- 弯路记录:CCM/HSV/钳位空间扣除/灰世界增益全试过——乘性增益修不了加性污染;纯净图可改发 Y 灰度。
- 教训:hbn_isp_api(libvpf)的 `hbn_isp_set_awb_attr` 需在出流后调用(启动即调返回 -65545/零增益);
  `pkill -f` 的模式若出现在远程 bash -c 命令串里照样自杀,杀进程用 pid 或确保串内无该字样。

**Why:** 记录硬件换代事实(IMX219→双目 SC132GS)+ 两 CSI 已验证地址/ID/出图,避免下次重新摸索;
[[rdk-x5-robot-status]] 里"相机进展(IMX219 CSI0=i2c-6 0x10)"一节已过时,以本条为准。
**How to apply:** 相机相关任务用 sc132gs(非 imx219);双目走 i2c-6(0x32)+i2c-4(0x30);
标定读 i2c-6 0x50 EEPROM;bring-up 参考 `/app/multimedia_samples/vp_sensors/sc132gs`。
相关:[[rdk-x5-astra-depth]](原深度方案是 USB Orbbec Astra,现多了 CSI 双目立体这条路)。
