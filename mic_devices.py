# -*- coding: utf-8 -*-
"""麦克风设备发现 / 电平测试 / VAD 阈值校准（控制台与命令行共用）。

为什么需要这个模块
------------------
`recorder/config.json` 里的 `audio_device` 是一个**写死的 dshow 字符串**，
换麦克风要三件事一起做，漏掉任何一件都会出问题：

  1. 改 `audio_device` —— 否则录的还是旧设备（或直接起不来）
  2. 改 `voice_rms`     —— 不同麦克风增益差 3~10 倍，沿用旧阈值会让 VAD
                           要么"永远听不见"（阈值过高）要么"永远在录"
                           （阈值过低，静音也被当人声）
  3. 重启录制服务        —— VAD 的 ffmpeg 采集流在建线程时就绑定了设备名

本模块把这三件事拆成可被 GUI 调用的原子操作：枚举设备 → 测电平 → 算阈值。

采集路径与录制/VAD 完全一致（ffmpeg dshow → s16le → float32，
见 recorder/audio_stream.py），保证「这里测到的电平 = 线上实际电平」。

命令行用法
----------
  python mic_devices.py --list                     # 列出所有音频输入设备
  python mic_devices.py --test --sec 10            # 测当前配置设备的电平
  python mic_devices.py --test --device "麦克风名"  # 测指定设备
  python mic_devices.py --calibrate                # 校准（15s 安静 + 15s 说话）
  python mic_devices.py --calibrate --apply        # 校准并写回 config.json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

RECORDER_DIR = Path(r"C:/recorder/recorder")
if str(RECORDER_DIR) not in sys.path:
    sys.path.insert(0, str(RECORDER_DIR))

from audio_stream import DshowPcmStream, find_ffmpeg  # noqa: E402

DEFAULT_CONFIG = RECORDER_DIR / "config.json"

# ffmpeg 9.x 的设备行前缀是 [in#0 @ 0x...]，旧版是 [dshow @ 0x...]，
# 这里不锁死前缀，只认「带引号的名字 + (audio|video)」这个稳定结构。
_DEV_RE = re.compile(r'^\s*\[[^\]]+\]\s+"(.+?)"\s+\((audio|video)\)\s*$')
_ALT_RE = re.compile(r'^\s*\[[^\]]+\]\s+Alternative name "(.+?)"\s*$')

_CREATE_NO_WINDOW = 0x08000000  # subprocess.CREATE_NO_WINDOW（避免 pythonw 下闪黑框）


# ---------------------------------------------------------------- 基础工具

def _decode(raw: bytes) -> str:
    """ffmpeg 的 stderr 在 Windows 上实测是 UTF-8；留 GBK 兜底以防换构建。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def dshow_arg(name: str) -> str:
    """把设备名规范成 ffmpeg -i 参数：`audio=本机麦克风 (...)`。"""
    name = (name or "").strip()
    return name if name.startswith(("audio=", "video=")) else f"audio={name}"


def strip_dshow_arg(device: str) -> str:
    """从 `audio=xxx` 反解出设备友好名（用于下拉框回显）。"""
    d = (device or "").strip()
    return d.split("=", 1)[1] if d.startswith(("audio=", "video=")) else d


def rms_of(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(block ** 2))) if block.size else 0.0


def _pct(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), q)) if values else 0.0


# ---------------------------------------------------------------- 设备枚举

def list_input_devices(kind: str = "audio") -> list[dict]:
    """枚举 dshow 设备。返回 [{"name": 友好名, "alt": 备用名, "kind": "audio"}]。

    实测（本机 ffmpeg 9.0.1）：只有一个音频输入设备
    「本机麦克风 (适用于数字麦克风的英特尔® 智音技术)」。
    换 USB 麦克风后插上即可在此出现，无需重启系统。
    """
    try:
        ff = find_ffmpeg()
    except FileNotFoundError:
        return []
    try:
        p = subprocess.run(
            [ff, "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, timeout=30,
            creationflags=(_CREATE_NO_WINDOW if sys.platform == "win32" else 0))
    except Exception:  # noqa: BLE001
        return []

    out: list[dict] = []
    cur: dict | None = None
    for line in _decode(p.stderr).splitlines():
        m = _ALT_RE.match(line)
        if m:
            if cur is not None:
                cur["alt"] = m.group(1)   # 「Alternative name」跟在设备行后面
            continue
        m = _DEV_RE.match(line)
        if not m:
            continue
        if m.group(2) == kind:
            cur = {"name": m.group(1), "alt": "", "kind": m.group(2)}
            out.append(cur)
        else:
            cur = None
    return out


# ---------------------------------------------------------------- 电平监测

class LevelMonitor:
    """后台线程持续读一路 dshow 采集流，维护最近若干秒的 RMS 序列。

    用于「设备」页的实时电平柱：看一眼就知道这个麦克风有没有声、
    增益偏高还是偏低。与录制/VAD 走同一条采集路径，读数可直接类比。
    """

    def __init__(self, device: str, sample_rate: int = 16000,
                 block_sec: float = 0.1, history_sec: float = 30.0):
        self.device = dshow_arg(device)
        self.sample_rate = sample_rate
        self.block_sec = block_sec
        self._rms_hist: deque = deque(maxlen=max(1, int(history_sec / block_sec)))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str = ""

    # -- 生命周期 ----------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.error = ""
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="mic-level")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        try:
            with DshowPcmStream(self.device, sample_rate=self.sample_rate,
                                block_sec=self.block_sec) as stream:
                for block in stream.blocks():
                    if self._stop.is_set():
                        break
                    with self._lock:
                        self._rms_hist.append(rms_of(block))
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"

    # -- 读数 --------------------------------------------------------------
    def samples(self) -> list[float]:
        with self._lock:
            return list(self._rms_hist)

    def stats(self, seconds: float = 10.0) -> dict:
        """最近 seconds 秒的电平统计。voice_rms 为空时按 0.02 判人声占比。"""
        n = max(1, int(seconds / self.block_sec))
        vals = self.samples()[-n:]
        if not vals:
            return {"n": 0, "rms": 0.0, "median": 0.0, "p95": 0.0, "peak": 0.0}
        return {
            "n": len(vals),
            "rms": vals[-1],
            "median": _pct(vals, 50),
            "p95": _pct(vals, 95),
            "peak": max(vals),
        }


def test_device(device: str, seconds: float = 10.0,
                on_tick=None) -> dict:
    """独立测一路设备（阻塞 seconds 秒），返回电平统计 + 判读。

    on_tick(elapsed, rms) 可选，用于命令行实时打印。
    注意：ffmpeg 支持同一设备被多路同时采集（生产上 VAD 与录制两路长期并存），
    所以录制进行中测电平也不会打断录制。
    """
    mon = LevelMonitor(device)
    mon.start()
    t0 = time.monotonic()
    last = 0.0
    try:
        while time.monotonic() - t0 < seconds:
            time.sleep(0.1)
            if on_tick:
                el = time.monotonic() - t0
                if el - last >= 0.5:
                    last = el
                    on_tick(el, mon.samples()[-1] if mon.samples() else 0.0)
    finally:
        mon.stop()
    st = mon.stats(seconds)
    st["error"] = mon.error
    st["verdict"], st["advice"] = judge_level(st)
    return st


def judge_level(st: dict) -> tuple[str, str]:
    """按静音段电平给出人话判读。"""
    if st.get("error"):
        return "失败", f"采集失败：{st['error']}"
    if st.get("n", 0) == 0:
        return "无数据", "没有采到任何音频块，设备可能被独占或名称不对"
    med = st.get("median", 0.0)
    if med < 0.0005:
        return "近乎静音", "电平极低：设备选错、被静音，或系统输入音量为 0"
    if med < 0.003:
        return "偏安静", f"底噪 {med:.4f}：可用，但远离麦克风时 VAD 可能漏触发"
    if med < 0.05:
        return "正常", f"底噪 {med:.4f}：适合 VAD 检测"
    return "偏吵/爆音", f"底噪 {med:.4f}：环境噪声或增益过高，VAD 会持续触发"


# ---------------------------------------------------------------- 阈值校准

def suggest_voice_rms(quiet: list[float], voice: list[float]) -> float:
    """建议阈值 = sqrt(静音p95 × 人声p05)——对数域取中，两侧各留余量。

    与 recorder/vad_calibrate.py 完全同一套公式，保证两处结果可比。
    """
    if not quiet or not voice:
        return 0.02
    return float(np.sqrt(_pct(quiet, 95) * _pct(voice, 5)))


def calibrate(device: str, quiet_sec: float = 15.0, voice_sec: float = 15.0,
              on_phase=None, on_tick=None) -> dict:
    """两段式校准：前 quiet_sec 秒保持安静，后 voice_sec 秒正常说话。

    on_phase(text) 在换阶段时回调（GUI 用来提示"请开始说话"）。
    """
    mon = LevelMonitor(device)
    mon.start()
    t0 = time.monotonic()
    total = quiet_sec + voice_sec
    last = 0.0
    phase = "quiet"
    if on_phase:
        on_phase("quiet")
    try:
        while True:
            el = time.monotonic() - t0
            if el > total:
                break
            time.sleep(0.1)
            if phase == "quiet" and el >= quiet_sec:
                phase = "voice"
                if on_phase:
                    on_phase("voice")
            if on_tick and el - last >= 0.5:
                last = el
                vals = mon.samples()
                on_tick(el, vals[-1] if vals else 0.0, phase)
    finally:
        mon.stop()

    allv = mon.samples()
    nq = max(1, int(quiet_sec / mon.block_sec))
    quiet = allv[:nq]
    voice = allv[nq:]
    q_p95 = _pct(quiet, 95)
    v_p05 = _pct(voice, 5)
    v_med = _pct(voice, 50)
    suggest = suggest_voice_rms(quiet, voice)
    return {
        "device": dshow_arg(device),
        "quiet_median": _pct(quiet, 50),
        "quiet_p95": q_p95,
        "voice_p05": v_p05,
        "voice_median": v_med,
        "suggest": round(suggest, 5),
        "separation_ok": bool(v_med > q_p95 * 2),
        "error": mon.error,
    }


# ---------------------------------------------------------------- 配置读写

def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_audio_config(device: str, voice_rms: float | None,
                      path: Path = DEFAULT_CONFIG) -> None:
    """只改 audio_device / voice_rms 两个键，其余原样保留（原子替换）。"""
    path = Path(path)
    cfg = load_config(path)
    cfg["audio_device"] = dshow_arg(device)
    if voice_rms:
        cfg["voice_rms"] = round(float(voice_rms), 5)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="麦克风设备 / 电平 / 校准")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--list", action="store_true", help="列出音频输入设备")
    ap.add_argument("--test", action="store_true", help="测试电平")
    ap.add_argument("--calibrate", action="store_true", help="校准 VAD 阈值")
    ap.add_argument("--device", default="", help="设备名（省略则用 config.json）")
    ap.add_argument("--sec", type=float, default=10.0, help="测试时长")
    ap.add_argument("--quiet", type=float, default=15.0)
    ap.add_argument("--voice", type=float, default=15.0)
    ap.add_argument("--apply", action="store_true", help="把校准结果写回 config.json")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    cfg = load_config(cfg_path)
    device = args.device or cfg.get("audio_device", "")

    if args.list:
        devs = list_input_devices("audio")
        print(f"音频输入设备（{len(devs)} 个）：")
        for i, d in enumerate(devs, 1):
            cur = "  ← 当前配置" if d["name"] == strip_dshow_arg(device) else ""
            print(f"  {i}. {d['name']}{cur}")
            if d["alt"]:
                print(f"     备选名：{d['alt']}")
        if not devs:
            print("  （未发现任何音频输入设备，请检查麦克风是否接入）")
        return 0

    if args.test:
        print(f"设备：{device}\n测试 {args.sec:.0f} 秒…\n")
        st = test_device(device, args.sec,
                         on_tick=lambda el, v: print(f"  [{el:5.1f}s] RMS={v:.4f}"))
        print("\n----- 结果 -----")
        print(f"  采样块 {st['n']}　中位 {st['median']:.4f}　p95 {st['p95']:.4f}　"
              f"峰值 {st['peak']:.4f}")
        print(f"  判读：{st['verdict']} —— {st['advice']}")
        print(f"  当前 config.voice_rms = {cfg.get('voice_rms')}")
        return 0

    if args.calibrate:
        print(f"设备：{device}")
        print(f"前 {args.quiet:.0f} 秒请保持安静，之后 {args.voice:.0f} 秒请正常说话\n")
        for i in (3, 2, 1):
            print(f"  {i}…")
            time.sleep(1)
        res = calibrate(
            device, args.quiet, args.voice,
            on_phase=lambda p: print("\n>>> 现在请开始说话 <<<" if p == "voice" else ">>> 保持安静 <<<"),
            on_tick=lambda el, v, p: print(f"  [{el:5.1f}s] {'安静' if p == 'quiet' else '说话'} RMS={v:.4f}"))
        print("\n----- 校准结果 -----")
        print(f"  静音中位 {res['quiet_median']:.4f}　静音p95 {res['quiet_p95']:.4f}")
        print(f"  人声p05 {res['voice_p05']:.4f}　人声中位 {res['voice_median']:.4f}")
        print(f"  >> 建议 voice_rms = {res['suggest']}")
        if not res["separation_ok"]:
            print("  !! 人声与静音区分度不足：环境噪声过高或增益太低，建议改善后重测")
        old = cfg.get("voice_rms")
        if args.apply:
            save_audio_config(device, res["suggest"], cfg_path)
            print(f"  已写入 {cfg_path}：voice_rms {old} → {res['suggest']}")
            print("  （还需重启录制服务，或在控制台「麦克风」页点『保存并应用』热重载）")
        else:
            print(f"  当前 config.voice_rms = {old}（加 --apply 可写回）")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
