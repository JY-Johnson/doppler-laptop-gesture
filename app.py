"""Windows desktop MVP: use laptop audio Doppler shifts to turn pages.

The default mapping is deliberately small and safe:
  approach -> PageDown
  away     -> PageUp
  wave     -> visual/log notification only

Desktop key injection is disabled until the user checks the explicit control
checkbox in the window.
"""

from __future__ import annotations

import ctypes
import math
import os
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk
from typing import Optional

try:
    import numpy as np
    import sounddevice as sd
except ImportError as exc:
    np = None  # type: ignore[assignment]
    sd = None  # type: ignore[assignment]
    AUDIO_IMPORT_ERROR = exc
else:
    AUDIO_IMPORT_ERROR = None


F0 = 18_000.0
SAMPLE_RATE = 48_000
FFT_SIZE = 32_768
BLOCK_SIZE = 2_048
BAND_MIN = 35.0
BAND_MAX = 360.0
CALIBRATION_SECONDS = 1.8


@dataclass(frozen=True)
class GestureEvent:
    kind: str
    label: str
    at: float


@dataclass(frozen=True)
class Snapshot:
    status: str = "未启动"
    meter: float = 0.0
    peak_offset: float = 0.0
    peak_db: float = -110.0
    offsets: tuple[float, ...] = ()
    spectrum_db: tuple[float, ...] = ()


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return -110.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


class DopplerDetector:
    """FFT detector with noise calibration, smoothing and conservative gestures."""

    def __init__(self, sensitivity: int = 3) -> None:
        if np is None:
            raise RuntimeError(f"缺少音频依赖：{AUDIO_IMPORT_ERROR}")
        self.sensitivity = max(1, min(10, sensitivity))
        self.freqs = np.fft.rfftfreq(FFT_SIZE, 1.0 / SAMPLE_RATE)
        self.window = np.hanning(FFT_SIZE).astype(np.float32)
        self.high_mask = (self.freqs >= F0 + BAND_MIN) & (self.freqs <= F0 + BAND_MAX)
        self.low_mask = (self.freqs >= F0 - BAND_MAX) & (self.freqs <= F0 - BAND_MIN)
        self.display_mask = (self.freqs >= F0 - 420) & (self.freqs <= F0 + 420)
        self.display_offsets = tuple((self.freqs[self.display_mask] - F0).tolist())
        self.ring = np.zeros(FFT_SIZE, dtype=np.float32)
        self.filled = 0
        self.calibration_started = time.monotonic()
        self.calibration_values: list[tuple[float, float]] = []
        self.baseline_high_db = -110.0
        self.baseline_low_db = -110.0
        self.noise_high_db = -110.0
        self.noise_low_db = -110.0
        self.high_ema = 0.0
        self.low_ema = 0.0
        self.candidate_direction: Optional[str] = None
        self.candidate_frames = 0
        self.wave_candidate: Optional[str] = None
        self.wave_frames = 0
        self.wave_last: Optional[str] = None
        self.wave_last_at = 0.0
        self.wave_neutral_frames = 0
        self.wave_awaiting_reverse = False
        self.cooldown_until = 0.0
        self.last_snapshot = Snapshot(status="等待音频缓冲…")

    @staticmethod
    def power_to_db(power: float) -> float:
        return 10.0 * math.log10(max(float(power), 1e-20))

    def set_sensitivity(self, value: int) -> None:
        self.sensitivity = max(1, min(10, int(value)))

    def _calculate_spectrum(self):
        transformed = np.fft.rfft(self.ring * self.window)
        power = (np.abs(transformed) ** 2) / FFT_SIZE
        high_db = self.power_to_db(float(np.mean(power[self.high_mask])))
        low_db = self.power_to_db(float(np.mean(power[self.low_mask])))
        display_db = 10.0 * np.log10(np.maximum(power[self.display_mask], 1e-20))
        sideband_mask = (np.abs(self.freqs - F0) >= BAND_MIN) & (np.abs(self.freqs - F0) <= BAND_MAX)
        indices = np.flatnonzero(sideband_mask)
        peak_index = indices[int(np.argmax(power[sideband_mask]))]
        peak_offset = float(self.freqs[peak_index] - F0)
        peak_db = float(10.0 * np.log10(max(power[peak_index], 1e-20)))
        return display_db, high_db, low_db, peak_offset, peak_db

    def _make_snapshot(self, status: str, meter: float, display_db, peak_offset: float, peak_db: float) -> Snapshot:
        self.last_snapshot = Snapshot(
            status=status,
            meter=max(0.0, min(100.0, meter)),
            peak_offset=peak_offset,
            peak_db=peak_db,
            offsets=self.display_offsets,
            spectrum_db=tuple(display_db.tolist()),
        )
        return self.last_snapshot

    def _update_wave(self, now: float, high_threshold: float, low_threshold: float) -> Optional[GestureEvent]:
        # A wave requires a neutral valley and a strong opposite segment.
        wave_threshold = max(4.2, min(high_threshold, low_threshold) - 0.6)
        delta = self.high_ema - self.low_ema
        strength = max(self.high_ema, self.low_ema)
        direction = "靠近" if strength > wave_threshold and delta > 2.2 else "远离" if strength > wave_threshold and delta < -2.2 else None

        if self.wave_last and now - self.wave_last_at > 1.5:
            self.wave_last = None
            self.wave_awaiting_reverse = False
        if direction is None:
            self.wave_candidate = None
            self.wave_frames = 0
            if self.wave_last:
                self.wave_neutral_frames += 1
                if self.wave_neutral_frames >= 3:
                    self.wave_awaiting_reverse = True
            return None

        self.wave_neutral_frames = 0
        if self.wave_candidate == direction:
            self.wave_frames += 1
        else:
            self.wave_candidate = direction
            self.wave_frames = 1
        if self.wave_frames < 6:
            return None

        if self.wave_last and self.wave_last != direction and self.wave_awaiting_reverse and now - self.wave_last_at <= 1.5:
            previous = self.wave_last
            self.wave_last = None
            self.wave_last_at = 0.0
            self.wave_candidate = None
            self.wave_frames = 0
            self.wave_neutral_frames = 0
            self.wave_awaiting_reverse = False
            self.candidate_direction = None
            self.candidate_frames = 0
            self.cooldown_until = now + 0.7
            return GestureEvent("wave", f"挥动（{previous} → {direction}）", now)

        if not self.wave_last or self.wave_last != direction:
            self.wave_last = direction
            self.wave_last_at = now
            self.wave_awaiting_reverse = False
        self.wave_frames = 0
        return None

    def process(self, block: np.ndarray) -> tuple[Snapshot, Optional[GestureEvent]]:
        block = np.asarray(block, dtype=np.float32).reshape(-1)
        if block.size == 0:
            return self.last_snapshot, None
        if block.size >= FFT_SIZE:
            self.ring[:] = block[-FFT_SIZE:]
            self.filled = FFT_SIZE
        else:
            self.ring[:-block.size] = self.ring[block.size:]
            self.ring[-block.size:] = block
            self.filled = min(FFT_SIZE, self.filled + block.size)
        if self.filled < FFT_SIZE:
            empty = np.full(len(self.display_offsets), -110.0)
            return self._make_snapshot("等待音频缓冲…", 0, empty, 0, -110), None

        display_db, high_db, low_db, peak_offset, peak_db = self._calculate_spectrum()
        now = time.monotonic()
        if now - self.calibration_started < CALIBRATION_SECONDS:
            self.calibration_values.append((high_db, low_db))
            progress = (now - self.calibration_started) / CALIBRATION_SECONDS * 100
            return self._make_snapshot(f"校准中 {min(100, progress):.0f}%：请保持手不动", 0, display_db, peak_offset, peak_db), None

        if self.calibration_values:
            high_values = [item[0] for item in self.calibration_values]
            low_values = [item[1] for item in self.calibration_values]
            self.baseline_high_db = percentile(high_values, 0.50)
            self.baseline_low_db = percentile(low_values, 0.50)
            self.noise_high_db = percentile(high_values, 0.90)
            self.noise_low_db = percentile(low_values, 0.90)
            self.calibration_values.clear()

        self.high_ema = self.high_ema * 0.78 + (high_db - self.baseline_high_db) * 0.22
        self.low_ema = self.low_ema * 0.78 + (low_db - self.baseline_low_db) * 0.22
        margin = 5.8 - (self.sensitivity - 1) * 0.35
        high_threshold = max(3.5, self.noise_high_db - self.baseline_high_db + margin)
        low_threshold = max(3.5, self.noise_low_db - self.baseline_low_db + margin)
        gap = 3.0
        high_active = self.high_ema > high_threshold and self.high_ema - self.low_ema > gap
        low_active = self.low_ema > low_threshold and self.low_ema - self.high_ema > gap
        meter = max(self.high_ema, self.low_ema) / max(high_threshold, low_threshold) * 100

        wave_event = self._update_wave(now, high_threshold, low_threshold)
        if wave_event:
            return self._make_snapshot(wave_event.label, meter, display_db, peak_offset, peak_db), wave_event

        direction = "靠近" if high_active else "远离" if low_active else None
        if direction is None:
            self.candidate_direction = None
            self.candidate_frames = 0
            return self._make_snapshot("可以做手势", meter, display_db, peak_offset, peak_db), None
        if now < self.cooldown_until:
            return self._make_snapshot("动作冷却中…", meter, display_db, peak_offset, peak_db), None
        if self.candidate_direction == direction:
            self.candidate_frames += 1
        else:
            self.candidate_direction = direction
            self.candidate_frames = 1
        if self.candidate_frames < 8:
            return self._make_snapshot("检测中…", meter, display_db, peak_offset, peak_db), None

        self.candidate_direction = None
        self.candidate_frames = 0
        self.cooldown_until = now + 0.9
        kind = "approach" if direction == "靠近" else "away"
        event = GestureEvent(kind, direction, now)
        return self._make_snapshot(direction, meter, display_db, peak_offset, peak_db), event


class AudioEngine:
    def __init__(self, detector: DopplerDetector, gain: float) -> None:
        if sd is None or np is None:
            raise RuntimeError(f"缺少音频依赖：{AUDIO_IMPORT_ERROR}")
        self.detector = detector
        self.gain = max(0.003, min(0.08, gain))
        self.blocks: queue.Queue[np.ndarray] = queue.Queue(maxsize=12)
        self.events: queue.Queue[GestureEvent] = queue.Queue()
        self.stop_event = threading.Event()
        self.stream = None
        self.worker: Optional[threading.Thread] = None
        self.phase = 0
        self.status_text = ""
        self.snapshot = detector.last_snapshot
        self.lock = threading.Lock()

    def callback(self, indata, outdata, frames, _time_info, status) -> None:
        if status:
            self.status_text = str(status)
        indexes = np.arange(frames, dtype=np.float32) + self.phase
        outdata[:, 0] = (self.gain * np.sin(2 * np.pi * F0 * indexes / SAMPLE_RATE)).astype(np.float32)
        self.phase = (self.phase + frames) % SAMPLE_RATE
        try:
            self.blocks.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                block = self.blocks.get(timeout=0.2)
            except queue.Empty:
                continue
            snapshot, event = self.detector.process(block)
            with self.lock:
                self.snapshot = snapshot
            if event:
                self.events.put(event)

    def start(self) -> None:
        self.stop_event.clear()
        self.stream = sd.Stream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            channels=(1, 1),
            dtype="float32",
            latency="low",
            callback=self.callback,
        )
        self.stream.start()
        self.worker = threading.Thread(target=self.worker_loop, daemon=True)
        self.worker.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            finally:
                self.stream = None
        if self.worker is not None:
            self.worker.join(timeout=1.0)
            self.worker = None

    def current_snapshot(self) -> Snapshot:
        with self.lock:
            return self.snapshot


if os.name == "nt":
    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("ki", KEYBDINPUT)]


class PageController:
    def send(self, direction: str) -> None:
        if os.name != "nt":
            raise RuntimeError("桌面翻页控制只支持 Windows")
        vk = 0x22 if direction == "down" else 0x21
        key_down = INPUT(type=1, ki=KEYBDINPUT(vk, 0, 0, 0, 0))
        key_up = INPUT(type=1, ki=KEYBDINPUT(vk, 0, 2, 0, 0))
        user32 = ctypes.windll.user32
        if user32.SendInput(1, ctypes.byref(key_down), ctypes.sizeof(INPUT)) != 1:
            raise ctypes.WinError()
        if user32.SendInput(1, ctypes.byref(key_up), ctypes.sizeof(INPUT)) != 1:
            raise ctypes.WinError()


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Doppler 翻页控制")
        self.root.geometry("940x680")
        self.root.minsize(780, 580)
        self.detector: Optional[DopplerDetector] = None
        self.engine: Optional[AudioEngine] = None
        self.controller = PageController()
        self.running = False
        self.sensitivity = tk.IntVar(value=3)
        self.gain = tk.DoubleVar(value=0.02)
        self.page_control = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="未启动")
        self.detail = tk.StringVar(value="桌面翻页默认关闭；启动后请先完成校准。")
        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(60, self.poll)

    def build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Doppler 翻页控制", font=("Segoe UI", 20, "bold")).pack(anchor="w")
        ttk.Label(outer, text="靠近 → PageDown，远离 → PageUp；挥动只显示事件，不执行翻页。").pack(anchor="w", pady=(2, 12))

        controls = ttk.LabelFrame(outer, text="控制", padding=10)
        controls.pack(fill="x")
        self.start_button = ttk.Button(controls, text="开始校准并启动", command=self.start)
        self.start_button.grid(row=0, column=0, padx=(0, 8), pady=4)
        self.stop_button = ttk.Button(controls, text="停止", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=1, padx=(0, 18), pady=4)
        ttk.Label(controls, text="灵敏度").grid(row=0, column=2, sticky="e")
        ttk.Scale(controls, from_=1, to=10, variable=self.sensitivity, orient="horizontal", length=150).grid(row=0, column=3, padx=8)
        ttk.Label(controls, textvariable=self.sensitivity, width=3).grid(row=0, column=4)
        ttk.Label(controls, text="载波音量").grid(row=0, column=5, padx=(18, 0), sticky="e")
        ttk.Scale(controls, from_=0.005, to=0.06, variable=self.gain, orient="horizontal", length=150).grid(row=0, column=6, padx=8)
        ttk.Checkbutton(controls, text="启用桌面翻页（当前前台窗口）", variable=self.page_control, command=self.control_changed).grid(row=1, column=0, columnspan=7, sticky="w", pady=(8, 0))

        ttk.Label(outer, textvariable=self.status, font=("Segoe UI", 17, "bold")).pack(anchor="w", pady=(12, 2))
        self.meter = ttk.Progressbar(outer, maximum=100, mode="determinate")
        self.meter.pack(fill="x", pady=(0, 6))
        ttk.Label(outer, textvariable=self.detail, wraplength=880).pack(anchor="w")

        spectrum_box = ttk.LabelFrame(outer, text="18 kHz 附近实时频谱", padding=8)
        spectrum_box.pack(fill="both", expand=True, pady=(14, 0))
        self.canvas = tk.Canvas(spectrum_box, height=270, background="#101722", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        events_box = ttk.LabelFrame(outer, text="最近事件", padding=8)
        events_box.pack(fill="x", pady=(12, 0))
        self.events = tk.Listbox(events_box, height=4)
        self.events.pack(fill="x")

    def control_changed(self) -> None:
        self.detail.set("桌面翻页已开启：靠近发送 PageDown，远离发送 PageUp。" if self.page_control.get() else "桌面翻页已关闭，只显示检测结果，不发送按键。")

    def start(self) -> None:
        if self.running:
            return
        if np is None or sd is None:
            messagebox.showerror("缺少依赖", f"请先安装 requirements.txt：\n{AUDIO_IMPORT_ERROR}")
            return
        try:
            self.detector = DopplerDetector(self.sensitivity.get())
            self.engine = AudioEngine(self.detector, self.gain.get())
            self.engine.start()
        except Exception as exc:
            self.engine = None
            self.detector = None
            messagebox.showerror("音频启动失败", str(exc))
            return
        self.running = True
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status.set("正在启动音频…")

    def stop(self) -> None:
        if self.engine is not None:
            self.engine.stop()
        self.engine = None
        self.detector = None
        self.running = False
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status.set("已停止")
        self.meter.configure(value=0)

    def add_event(self, text: str) -> None:
        self.events.insert(0, f"{time.strftime('%H:%M:%S')}  {text}")
        while self.events.size() > 8:
            self.events.delete(8)

    def handle_event(self, event: GestureEvent) -> None:
        if event.kind == "wave":
            self.add_event(event.label)
            return
        if not self.page_control.get():
            self.add_event(f"{event.label}（翻页关闭）")
            return
        try:
            self.controller.send("down" if event.kind == "approach" else "up")
            self.add_event(f"{event.label}  →  {'PageDown' if event.kind == 'approach' else 'PageUp'}")
        except Exception as exc:
            self.add_event(f"{event.label}  按键失败：{exc}")

    def draw_spectrum(self, snapshot: Snapshot) -> None:
        self.canvas.delete("all")
        width = max(500, self.canvas.winfo_width())
        height = max(200, self.canvas.winfo_height())
        left, top, right, bottom = 48, 20, 16, 34
        plot_width, plot_height = width - left - right, height - top - bottom
        min_offset, max_offset, min_db, max_db = -420.0, 420.0, -110.0, -20.0

        def x_for(offset: float) -> float:
            return left + (offset - min_offset) / (max_offset - min_offset) * plot_width

        def y_for(db: float) -> float:
            return top + (max_db - max(min_db, min(max_db, db))) / (max_db - min_db) * plot_height

        self.canvas.create_rectangle(0, 0, width, height, fill="#101722", outline="")
        self.canvas.create_rectangle(x_for(35), top, x_for(max_offset), top + plot_height, fill="#10283c", outline="")
        self.canvas.create_rectangle(x_for(min_offset), top, x_for(-35), top + plot_height, fill="#302714", outline="")
        for db in range(-100, -19, 20):
            y = y_for(db)
            self.canvas.create_line(left, y, width - right, y, fill="#293849")
            self.canvas.create_text(5, y, text=f"{db} dB", anchor="w", fill="#8092a6", font=("Segoe UI", 9))
        for offset in (-360, -180, 0, 180, 360):
            x = x_for(offset)
            self.canvas.create_line(x, top, x, top + plot_height, fill="#435365", dash=(3, 4))
            self.canvas.create_text(x, height - 18, text=f"{offset:+d}" if offset else "0", fill="#b4c1cf", font=("Segoe UI", 9))
        carrier_x = x_for(0)
        self.canvas.create_line(carrier_x, top, carrier_x, top + plot_height, fill="#f1f5f9", dash=(4, 4))
        self.canvas.create_text(carrier_x + 5, top + 8, text="18 kHz", anchor="w", fill="#f1f5f9", font=("Segoe UI", 9))
        if snapshot.offsets and snapshot.spectrum_db:
            points: list[float] = []
            for offset, db in zip(snapshot.offsets, snapshot.spectrum_db):
                points.extend((x_for(offset), y_for(db)))
            if len(points) >= 4:
                self.canvas.create_line(*points, fill="#67c9ff", width=2, smooth=True)
        if snapshot.spectrum_db and snapshot.peak_db > -105:
            x, y = x_for(snapshot.peak_offset), y_for(snapshot.peak_db)
            self.canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#fbbf24", outline="")
            self.canvas.create_text(width - 8, top + 10, text=f"峰值 Δf {snapshot.peak_offset:+.0f} Hz", anchor="e", fill="#7dd3fc", font=("Segoe UI", 10))

    def poll(self) -> None:
        if self.engine is not None:
            snapshot = self.engine.current_snapshot()
            self.status.set(snapshot.status)
            self.meter.configure(value=snapshot.meter)
            self.draw_spectrum(snapshot)
            while True:
                try:
                    event = self.engine.events.get_nowait()
                except queue.Empty:
                    break
                self.handle_event(event)
        else:
            self.draw_spectrum(Snapshot())
        if self.detector is not None:
            self.detector.set_sensitivity(self.sensitivity.get())
        self.root.after(60, self.poll)

    def close(self) -> None:
        self.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
