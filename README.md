# Doppler Laptop Gesture

让普通笔记本的扬声器和麦克风变成一个简单的手势传感器。

这个项目利用扬声器发出的约 18 kHz 载波，分析手部反射声产生的 Doppler 频移，用来判断手是在靠近还是远离设备，并进一步控制当前前台窗口连续翻页。

它不需要摄像头、手环或额外传感器，包含一个浏览器实验页面和一个 Windows 桌面客户端。

> Experimental prototype — this project is for interaction experiments and learning, not a production-grade gesture-recognition system.

## What it can do

### Browser experiment

- 实时显示 18 kHz 附近的频谱
- 校准环境噪声和直达声
- 观察靠近、远离对应的频率变化
- 用动画展示声波、反射路径和当前判定
- 纯本地浏览器处理，不上传麦克风音频

### Windows page-turning client

- 检测到靠近：持续发送 `PageDown`
- 检测到远离：持续发送 `PageUp`
- 检测到挥手：停止连续翻页
- 支持调节连续翻页间隔，默认 `300 ms`
- 默认灵敏度为 `7`
- 桌面按键控制默认关闭，必须手动开启
- 按键发送到检测时的当前前台窗口

适合用于浏览器文章、PDF、演示文稿和图片查看器等场景。

## How it works

```text
扬声器播放 18 kHz 载波
        ↓
手部反射，产生频率偏移
        ↓
麦克风采集音频
        ↓
FFT 频谱分析 + 噪声地板校准
        ↓
靠近 / 远离 / 挥手状态机
        ↓
可选发送 PageDown / PageUp
```

检测器会对高低频侧带进行校准、平滑和连续帧确认，避免单个噪声尖峰触发操作。连续翻页运行时会启用更灵敏的挥手中断模式。

## Run the browser demo

在仓库目录打开 PowerShell：

```powershell
py -m http.server 8765 --directory .
```

浏览器访问 <http://localhost:8765/doppler-gesture.html>，允许麦克风权限，然后点击“开始校准并启动”。校准期间请保持手不动，并把手移出检测区。

## Run the Windows app

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

使用步骤：

1. 启动软件并等待校准完成。
2. 勾选“启用桌面翻页”。
3. 切换到浏览器、PDF 或 PPT 窗口。
4. 做靠近或远离动作，启动连续翻页。
5. 做一次反向挥动，停止连续翻页。

如果只想测试按键注入，可以使用“3 秒后测试 PageDown”，并在倒计时内切换到目标窗口。

## Build a Windows executable

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build.ps1
```

生成：

```text
dist\DopplerGesture.exe
```

## Project structure

```text
app.py                  # Windows 桌面客户端、音频检测和 PageUp/PageDown 控制
doppler-gesture.html    # 浏览器演示、动画和实时频谱
requirements.txt        # 运行依赖
requirements-build.txt  # 打包依赖
build.ps1               # PyInstaller 打包脚本
```

## Limitations

- 不同笔记本的扬声器、麦克风、采样率和系统音频增强差异很大。
- 18 kHz 接近人耳听觉上限，但并非所有设备都能稳定播放或采集。
- 单个扬声器和麦克风难以可靠识别纯左右移动；前后方向的径向运动更适合 Doppler 检测。
- 回声消除、自动增益、环境噪声和耳机输出都会影响结果。
- 18 kHz 音量应保持较低，避免长时间高音量播放。
- 桌面控制只作用于当前前台窗口，请先确认目标窗口正确获得焦点。

## Roadmap

- 增加设备兼容性检测和自动参数推荐
- 记录多设备测试数据，提供准确率和延迟指标
- 支持更多可配置的桌面快捷键
- 探索短 chirp、匹配滤波和更稳健的运动估计

## License

MIT License，见 [LICENSE](LICENSE)。
