---
name: rdk-x5-follow-me
description: Follow-me 相机+雷达融合跟随已实测可用;GUI systemd 开关;算法细节与实战教训
metadata:
  type: project
---

Follow-me 融合跟随(v3 2026-07-12,实测可用,详见 docs/rdk-x5-follow-me-fusion.html):

- **架构**:相机(BPU 感知 30FPS,身份/手势/方位)+ 雷达(腿聚类 10Hz,精确几何)双通道
  常开并行,非模式切换;控制 10~40Hz,麦轮矢量速度 vx=v·cosθ/vy=v·sinθ 直指主人,
  PD 转向(KW 4 + 变化率前馈 0.6);速度 0.5 直行→0.8(角度大加速);雷达距离基准
  ref_dist 锁定后首次雷达命中记录。
- **认腿=运动判别**(世界系,/odom 抵消自身运动):新咬合要求"1.2s 前不在原地",
  持有中"3s 世界系静止且相机黑"即放弃——桌凳腿尺寸过滤不掉,只能靠不动淘汰。
- **开关**:follow-me.service + GUI 仪表盘 🧍跟随 滑块(sw 样式),
  systemctl enable/disable --now 持久化,小车重启自动恢复。
- **实战教训**:①手势分类闪烁(11,0,0,11…)→滑窗投票,别用连续帧;②感知输出每 roi
  一条独立 target,手势在 hand-only target 上要按包含关系归属;③MOT min_score 0.8
  转身就断 ID,降 0.3;④速度沿车头发=方向错,麦轮必须矢量化;⑤systemd 下
  trap 'kill 0' 自杀成重启环、无 HOME 则 ROS 日志目录崩,service 要给 HOME 和
  ROS_LOG_DIR;⑥pgrep -f 会匹配含关键词的 ssh 命令自身(多次上当);
  ⑦开机相机/BPU 未就绪链条秒挂,默认 5 次/10s burst limit 把单元打成 failed
  ="重启后开关自己灭"——2026-07-13 修 Restart=always + StartLimitIntervalSec=0
  + TimeoutStopSec=25(感知链 10s 停不完曾被 SIGKILL);⑧手离相机太近会被
  hand_lmk 尺寸过滤直接丢弃("Move hand far from sensor!"),手势要在 1.5-2m 外做。
- 待做:故障注入测试、物理停车距离回填 FRONT_STOP。

**双目相机适配(2026-07-20,链路板上验证,跟随行为待实测)**:
- follow_start.sh 用 EEPROM "UNION" 探测(同 camera_autodetect)选相机:GS130WI →
  感知链吃 `/image_color_nv12`(544×640 右眼半分辨率,~20fps 源,BPU 全链跑起后 mono2d
  实测 6-8fps);否则回落 `/image_raw`。几何经环境变量注入 follow_me.py
  (FOLLOW_IMG_W/FX/CX/SHOULDER=544/333.25/263.4/150,=出厂标定 666.5/526.8 的一半;
  px_to_bearing 改 atan2((cx-CX)/FX),旧 IMX219 默认值兜底)。
- **深度测距融合**:stereo_combine(C++)订 mono2d body roi × stereonet 深度图,
  用 maps_[0](rectified→source 映射表)一遍平扫找"落在 roi 内的深度像素",
  30 分位(前景)发 `/follow/cam_ranges`(`[id,cx,range_m]` 三元组,小消息 rclpy 可安全订)。
  follow_me 按 **cx 几何匹配**(不信 track id——mono2d 与手势链各自跑 MOT,id 不同源!),
  深度距离优先于肩宽估距;无腿时用 KV_LEG 增益做真实距离闭环;ref_dist 也可由深度初始化。
- 注意:follow 链全开时板 load 可到 20+,mono2d 从 14fps 掉到 6-8fps,仍够用。

相关:[[rdk-x5-robot-status]] [[rdk-x5-nav2-plan]] [[feedback-atomic-commits]]
