---
name: rdk-x5-ci1302-voice
description: CI1302 语音模块能力边界(无宽泛音频IO/mp3不可播)、当前USB只供电未枚举、i2c-5 0x0d是罗盘非语音模块、全向麦方案文档
metadata:
  type: project
---

小车装了亚博 AI 语音交互模块(启英泰伦 **CI1302** + STC8H 桥接,资料在 `docs/temp/ai音频`,
用 mhtml-extract skill 解析)。2026-07-18 结论:

- **封闭关键词对讲机,无宽泛音频 IO**:麦克风音频只进芯片做离线识别(命中→5 字节帧
  `AA FF <type> <id> FB` 走串口/IIC);喇叭只能播固件预烧 TTS 词条(≤300 条,改词=
  启英泰伦云平台重做固件+烧录)。**任意 mp3/音频流物理上放不了**。
- **当前接线**:Type-C 只供了电,USB 上从未枚举(dmesg 无新设备)——要协议通信需换带
  数据的线(滑动开关拨右=STC8 串口,会出第二个 ttyUSB)或杜邦线接 40-pin IIC(i2c-5)。
- **坑**:i2c-5 上的 `0x0d` 是底盘罗盘 QMC5883L,不是语音模块;`0x3c` 是扩展板 OLED。
  ttyUSB0=底盘驱动(Mcnamu),ttyACM0=MS200 雷达,都被占。
- **全模态大模型音频通道**:方案定为 USB 全向麦音箱一体(UAC 免驱+硬件 AEC),设计文档
  `docs/usb-speakerphone-audio-design.html`;CI1302 降级为离线唤醒词/应急命令辅通道。
  板载 ES8326 codec(card0)有耳机座但没接喇叭/麦,可做备用输出。

**Why:** 避免再次误判该模块能做音频透传,或再往罗盘地址发播报指令。
**How to apply:** 音频相关需求一律走 USB 声卡通道;CI1302 只做唤醒/固定命令。相关:
[[rdk-x5-astra-depth]](同一 USB2 总线/供电 hub 约束)。
