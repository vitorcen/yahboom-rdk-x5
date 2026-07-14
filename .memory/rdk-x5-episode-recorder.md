---
name: rdk-x5-episode-recorder
description: 手柄/GUI 一键 rosbag2 录制 episode + episode_lab 分析;toggle/stop 分离等评审教训
metadata:
  type: project
---

Episode 录制系统(2026-07-13,板端 episode_recorder.py + 工作站 episode_lab.ipynb,实录验证):

- **形态**:板端常驻节点照抄 safety_stop 的"节点持状态 + /record_toggle(Empty) 翻转 +
  latched /recording(Bool) 镜像"机制;手柄 START 键与 GUI ⏺录制按钮同入口。
  录到 `/home/sunrise/episodes/ep_时间戳/`(bag + meta.yaml),录制中带 `.partial` 后缀,
  收尾成功才摘名——残包一眼可辨。
- **评审关键修订(codex/gpt-5.6)**:全停链路必须走**幂等 /record_stop**,不能发 toggle
  ——未录制时 toggle 会反向开录;/tf_static、/safety_enabled 要 --qos-profile-overrides-path
  显式 transient_local,否则 latched 历史录不进(实测 tf_static 2 条已录上);
  切包 1GiB 而非 512MB(高负载 split 有丢消息风险 rosbag2#2108);标注写独立
  annotations.yaml,meta.yaml 与原始 bag 不可变。
- **录全四级 cmd_vel**(joy/mux/drv/cmd_vel + follow):每条 episode 自带"谁在开车、
  mux 裁了啥、safety_stop 压了多少"证据链。图像 /image_jpeg 实测 ~28Hz,42s ≈ 46MB。
- **守门**:开录前磁盘余量 <4GB 拒绝(五连急促滴)+ 录制中 30s 复查自动停;
  开录前查话题图,缺的记进 meta 的 missing_topics,不静默缺列。
- **蜂鸣**:开录三短滴,停录一短一长(与急刹两短/一长不冲突)。
- **数据流单向**:板=采集缓存,rsync 拉回工作站=资产;CSV/JPEG 导出脚本灌到板上跑
  (rosbag2_py + deserialize_message,**sqlite blob 是 CDR 包着的,不是裸 JPEG**)。
- **不在工作站录的原因**:Twist/Joy 无 header,bag 记到达时间,Wi-Fi 抖动直接污染
  观测-动作对齐;best-effort 丢包成数据洞;双机双时钟混一个 bag。
- 手柄 START = buttons[11](2026-07-13 上板抓包实测;占位猜 9 是错的);
  见 [[rdk-x5-gamepad-teleop]] 的"勿信惯例"教训。

相关:[[rdk-x5-safety-stop]] [[rdk-x5-gamepad-teleop]] [[rdk-x5-nav2-plan]]
