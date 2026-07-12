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
  ROS_LOG_DIR;⑥pgrep -f 会匹配含关键词的 ssh 命令自身(多次上当)。
- 待做:故障注入测试、物理停车距离回填 FRONT_STOP。

相关:[[rdk-x5-robot-status]] [[rdk-x5-nav2-plan]] [[feedback-atomic-commits]]
