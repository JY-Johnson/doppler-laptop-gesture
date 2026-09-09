# Doppler Laptop Gesture

一个只依赖笔记本扬声器和麦克风的浏览器手势感知实验原型。页面播放约 18 kHz 载波，分析麦克风收到的反射声频移，并把结果显示为实时频谱和示意动画。

> Experimental prototype — not a production-grade gesture-recognition system.

## Features

- 靠近：反射频率上移（`+Δf`）
- 远离：反射频率下移（`−Δf`）
- 前后方向挥动：方向反转并经过短暂中性间隔后触发
- 实时频率谱、噪声地板校准和误报抑制
- 不使用摄像头，音频处理在浏览器本地完成

## Run locally

在仓库目录打开 PowerShell：

```powershell
py -m http.server 8765 --directory .
```

浏览器访问 <http://localhost:8765/doppler-gesture.html>，允许麦克风权限，然后点击“开始校准并启动”。校准期间把手移出检测区；建议先将灵敏度设为 `2–3`，稳定后再提高。

## Important limitations

- 不同笔记本的扬声器、麦克风和系统音频增强差异很大。
- 单个扬声器/麦克风难以可靠区分纯左右移动；前后方向的径向运动更容易检测。
- 声卡不支持 18 kHz、回声消除、自动增益和环境噪声都可能影响结果。
- 18 kHz 音量应保持较低，避免长时间高音量播放。

## License

MIT License，见 [LICENSE](LICENSE)。
