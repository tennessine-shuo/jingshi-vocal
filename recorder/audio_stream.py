"""统一的麦克风采集入口：ffmpeg dshow → PCM s16le → float32 块（方案B · 教室端）。

背景：录制链路用 ffmpeg dshow 采集（recorder.py），而旧版 VAD/校准用
sounddevice 走系统默认设备——两者抓到的可能不是同一个麦克风（多通道阵列
设备的 channel 0 可能为静音），导致 VAD 永远"听不见"。
本模块让 VAD 与校准复用与录制完全相同的 ffmpeg dshow 采集路径，
保证"校准值 = 线上实际电平"。

依赖：ffmpeg（见 find_ffmpeg 的解析顺序）、numpy。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import numpy as np

_FFMPEG_FALLBACK = Path(r"C:\ffmpeg\bin\ffmpeg.exe")
# 安装包捆绑副本（<root>\ffmpeg\bin\ffmpeg.exe）——无前置环境的机器上唯一来源
_FFMPEG_BUNDLED = (Path(__file__).resolve().parent.parent
                   / "ffmpeg" / "bin" / "ffmpeg.exe")


def find_ffmpeg() -> str:
    """解析 ffmpeg 可执行文件：捆绑副本 → PATH → 装机教程的默认位置。"""
    if _FFMPEG_BUNDLED.exists():
        return str(_FFMPEG_BUNDLED)
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    if _FFMPEG_FALLBACK.exists():
        return str(_FFMPEG_FALLBACK)
    raise FileNotFoundError(
        "找不到 ffmpeg：请把 C:\\ffmpeg\\bin 加入 PATH，"
        "或确认 ffmpeg.exe 存在于该目录")


class DshowPcmStream:
    """从 ffmpeg dshow 持续读取 16kHz 单声道 PCM，按块产出 float32。

    用法：
        with DshowPcmStream("audio=麦克风名") as s:
            for block in s.blocks():      # block: np.float32, 长度 = 1600 (0.1s)
                ...
    """

    def __init__(self, device: str, fmt: str = "dshow",
                 sample_rate: int = 16000, block_sec: float = 0.1):
        self.device = device
        self.fmt = fmt
        self.sample_rate = sample_rate
        self.block_sec = block_sec
        self.block_samples = int(sample_rate * block_sec)
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> "DshowPcmStream":
        cmd = [
            find_ffmpeg(), "-loglevel", "error",
            "-f", self.fmt, "-i", self.device,
            "-ar", str(self.sample_rate), "-ac", "1",
            "-f", "s16le", "pipe:1",
        ]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NO_WINDOW
                           if os.name == "nt" else 0))
        return self

    def blocks(self):
        """生成器：持续产出 block_sec 秒一个的 float32 音频块。"""
        nbytes = self.block_samples * 2  # s16le = 2 字节/采样
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            raw = self._proc.stdout.read(nbytes)
            if not raw or len(raw) < nbytes:
                return
            yield np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    def __exit__(self, *exc) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
