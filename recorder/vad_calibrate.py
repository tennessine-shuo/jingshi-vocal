"""VAD 阈值校准工具：采集静音与人声样本，给出 voice_rms 建议值。

用法：python vad_calibrate.py [--config config.json] [--quiet 15] [--voice 15]

流程：
  1) 前 quiet 秒保持安静（记录静音 RMS）
  2) 后 voice 秒正常说话（记录人声 RMS）
  3) 给出建议阈值 = sqrt(静音p95 × 人声p05)（对数域中值），
     将结果写入 config.json 的 "voice_rms" 即可。

重要：采集走与录制/VAD 完全相同的 ffmpeg dshow 路径（见 audio_stream.py），
设备名默认从 config.json 的 audio_device 读取——校准到的就是线上实际电平。

依赖：ffmpeg、numpy（见 audio_stream.py）。
"""
import argparse
import json
import time
from statistics import median, quantiles

import numpy as np

from audio_stream import DshowPcmStream

SAMPLE_RATE = 16000
BLOCK_SEC = 0.1


def _rms(buf: np.ndarray) -> float:
    return float(np.sqrt(np.mean(buf ** 2)))


def main() -> None:
    ap = argparse.ArgumentParser(description="VAD 阈值校准")
    ap.add_argument("--config", default="config.json", help="config.json 路径")
    ap.add_argument("--quiet", type=float, default=15.0, help="安静阶段时长（秒）")
    ap.add_argument("--voice", type=float, default=15.0, help="说话阶段时长（秒）")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    device = cfg["audio_device"]
    fmt = cfg.get("audio_fmt", "dshow")

    print(f"采集设备：{device}")
    print(f"校准开始：前 {args.quiet:.0f} 秒请保持安静，后 {args.voice:.0f} 秒请正常说话")
    print("3 秒后开始采集...")
    time.sleep(3)

    quiet_rms: list[float] = []
    voice_rms: list[float] = []
    t0 = time.monotonic()
    last_print = 0.0
    total = args.quiet + args.voice

    with DshowPcmStream(device, fmt=fmt, sample_rate=SAMPLE_RATE,
                        block_sec=BLOCK_SEC) as stream:
        for block in stream.blocks():
            now = time.monotonic() - t0
            if now > total:
                break
            v = _rms(block)
            if now < args.quiet:
                quiet_rms.append(v)
            else:
                voice_rms.append(v)
            if now - last_print >= 1.0:
                last_print = now
                phase = "安静" if now < args.quiet else "说话"
                print(f"  [{now:5.1f}s] {phase} RMS={v:.4f}")

    q, v = quiet_rms, voice_rms
    if not q or not v:
        print("未采集到任何音频，请检查麦克风是否接入并被系统识别")
        return

    q_p95 = quantiles(q, n=20)[18]      # 静音第 95 百分位（高估噪声）
    v_p05 = quantiles(v, n=20)[0]       # 人声第 5 百分位（低估人声）
    v_med = median(v)
    suggest = float(np.sqrt(q_p95 * v_p05))     # 对数域中值，两侧各留余量

    print("\n========== 校准结果 ==========")
    print(f"  静音 RMS 中位数 : {median(q):.4f}  (峰值 {max(q):.4f})")
    print(f"  静音 p95        : {q_p95:.4f}")
    print(f"  人声 p05        : {v_p05:.4f}   (中位数 {v_med:.4f})")
    print(f"  >> 建议 voice_rms: {suggest:.4f}")

    if v_med < q_p95 * 2:
        print("  !! 警告：人声与静音区分度不足，可能是环境噪声过高或麦克风增益太低，")
        print("     建议改善环境噪声或调高麦克风增益后重测")
    else:
        print("  区分度良好，可放心使用")
    print(f"  将 config.json 的 \"voice_rms\" 改为 {suggest:.4f} 即可")


if __name__ == "__main__":
    main()
