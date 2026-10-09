"""VAD 人声检测：能量阈值法，后台线程实时检测（方案B · 教室端）。

作用：课表之外的第二触发源——检测到人声自动开始录制，静音持续超时自动停止。
实现用音频能量(RMS)阈值起步（轻量、零模型依赖）；后续可无缝换成 Silero VAD
（只需把块判定里"RMS > 阈值"换成模型推理输出的人声概率）。

重要：采集走与录制完全相同的 ffmpeg dshow 路径（见 audio_stream.py），
保证 VAD 听到的电平与录进文件的电平一致（旧版用 sounddevice 走系统默认
设备，多通道麦克风阵列下 channel 0 可能为静音，导致 VAD 永远听不见）。

依赖：ffmpeg、numpy（见 requirements.txt / audio_stream.py）。
"""
import logging
import threading
import time
from collections import deque

import numpy as np

from audio_stream import DshowPcmStream

log = logging.getLogger("vad")


class VadDetector(threading.Thread):
    """后台线程持续监听麦克风，统计"连续人声/连续静音"秒数。

    同时维护一段最近音频的环形缓冲（buffer_sec 秒），供"录音中实时声纹识别"
    复用本路采集 —— 这样识别不必再打开第三路 dshow，麦克风采集路数不增加。
    """

    def __init__(self, audio_device: str, audio_fmt: str = "dshow",
                 sample_rate: int = 16000, block_sec: float = 0.1,
                 voice_rms: float = 0.02, buffer_sec: float = 40.0,
                 level_history_sec: float = 30.0):
        super().__init__(daemon=True, name="vad-detector")
        self.audio_device = audio_device
        self.audio_fmt = audio_fmt
        self.sample_rate = sample_rate
        self.block_sec = block_sec
        self.voice_rms = voice_rms
        self.buffer_sec = buffer_sec
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._voice_sec = 0.0      # 连续有人声的秒数
        self._silence_sec = 0.0    # 连续静音的秒数
        self._stream: DshowPcmStream | None = None
        self.started_at: float = 0.0   # time.time()，供控制台算"这路采集活了多久"
        # 最近音频环形缓冲（deque 自动丢弃最旧的块）
        self._buf: deque = deque(maxlen=max(1, int(round(buffer_sec / block_sec))))
        # 最近电平序列（只存 RMS 标量，几十字节/秒）：
        # 控制台「麦克风」页的实时电平柱直接读它 —— 不必再开一路采集。
        self._rms_hist: deque = deque(
            maxlen=max(1, int(round(level_history_sec / block_sec))))

    def run(self) -> None:
        log.info("VAD 检测启动（阈值 RMS=%.3f，设备=%s）",
                 self.voice_rms, self.audio_device)
        self.started_at = time.time()
        try:
            with DshowPcmStream(self.audio_device, fmt=self.audio_fmt,
                                sample_rate=self.sample_rate,
                                block_sec=self.block_sec) as stream:
                self._stream = stream
                for block in stream.blocks():
                    if self._stop.is_set():
                        break
                    self._on_block(block)
        except Exception:
            log.exception("VAD 采集异常退出（课表触发不受影响）")
        finally:
            self._stream = None
            log.info("VAD 检测已停止")

    def _on_block(self, block: np.ndarray) -> None:
        """每 block_sec 秒处理一块：更新连续人声/静音计数 + 写入环形缓冲。"""
        level = float(np.sqrt(np.mean(block ** 2)))
        voice = bool(level > self.voice_rms)
        with self._lock:
            self._buf.append(block)
            self._rms_hist.append(level)
            if voice:
                self._voice_sec += self.block_sec
                self._silence_sec = 0.0
            else:
                self._silence_sec += self.block_sec
                self._voice_sec = 0.0

    def voice_seconds(self) -> float:
        """返回当前连续人声秒数（无人声则为 0）。"""
        with self._lock:
            return self._voice_sec

    def silence_seconds(self) -> float:
        """返回当前连续静音秒数（有人声则为 0）。"""
        with self._lock:
            return self._silence_sec

    def snapshot_recent(self, seconds: float) -> "np.ndarray | None":
        """返回最近 seconds 秒的音频（float32 一维）；缓冲不足则 None。

        供 recorder 做"开录后本地声纹识别"：直接复用 VAD 这一路采集，
        不用为了识别再打开一路麦克风。
        """
        need = max(1, int(round(seconds / self.block_sec)))
        with self._lock:
            if len(self._buf) < need:
                return None
            blocks = list(self._buf)[-need:]
        return np.concatenate(blocks)

    def voice_ratio_recent(self, seconds: float) -> float:
        """最近 seconds 秒里"人声块"占比（0~1）：判断这段音频值不值得做声纹。"""
        need = max(1, int(round(seconds / self.block_sec)))
        with self._lock:
            if len(self._buf) < need:
                return 0.0
            blocks = list(self._buf)[-need:]
        voiced = sum(1 for b in blocks
                     if float(np.sqrt(np.mean(b ** 2))) > self.voice_rms)
        return voiced / len(blocks)

    def level_stats(self, seconds: float = 10.0) -> dict:
        """最近 seconds 秒的电平统计（控制台实时电平柱 + 换麦前后对比用）。

        复用 VAD 这一路已有采集，**零新增麦克风占用** —— 所以控制台
        不用点"测试"也能一直看到电平，换麦克风前后的数字可直接对比。
        """
        need = max(1, int(round(seconds / self.block_sec)))
        with self._lock:
            vals = list(self._rms_hist)[-need:]
            alive = bool(self._stream is not None)
        if not vals:
            return {"n": 0, "rms": 0.0, "median": 0.0, "p95": 0.0,
                    "peak": 0.0, "stream": alive}
        arr = np.asarray(vals, dtype=np.float64)
        return {
            "n": len(vals),
            "rms": float(vals[-1]),
            "median": float(np.median(arr)),
            "p95": float(np.percentile(arr, 95)),
            "peak": float(arr.max()),
            "stream": alive,
        }

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=5)
