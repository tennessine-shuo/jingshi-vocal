# -*- coding: utf-8 -*-
"""探测 sherpa-onnx 声纹提取 API 的正确用法。"""
import sys
import wave

import numpy as np
import sherpa_onnx

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from _course import enroll_sample                        # noqa: E402

MODEL = r"C:\recorder\models\campplus_sv_zh_16k.onnx"
_s = enroll_sample("S002_*.wav") or enroll_sample("*.wav")
WAV = str(_s) if _s else r"C:\recorder\cloud_work\enroll_samples\<样本>.wav"

# ---- 1. 构造配置（探测允许的参数名）----
print("=== SpeakerEmbeddingExtractorConfig 构造 ===")
cfg = None
for kwargs in (
    dict(model=MODEL, num_threads=2),
    dict(model=MODEL),
):
    try:
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(**kwargs)
        print("  OK ->", kwargs)
        break
    except Exception as e:  # noqa: BLE001
        print("  失败:", kwargs, "->", e)

ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
print("  extractor.dim =", ex.dim)

# ---- 2. 读 wav → float32 [-1,1] ----
with wave.open(WAV) as w:
    sr = w.getframerate()
    n = w.getnframes()
    ch = w.getnchannels()
    raw = w.readframes(n)
print(f"=== 音频 {sr}Hz {ch}ch {n/sr:.1f}s ===")
samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

# ---- 3. 建流并喂数据 ----
stream = ex.create_stream()
print("=== stream 方法 ===")
print([m for m in dir(stream) if not m.startswith("_")])

stream.accept_waveform(sample_rate=sr, waveform=samples)
if hasattr(stream, "input_finished"):
    stream.input_finished()
    print("  input_finished() OK")

ok = ex.is_ready(stream)
print("  is_ready =", ok)

# ---- 4. 取 embedding（探测取值方式）----
emb = None
ret = ex.compute(stream)
print("  extractor.compute 返回:", type(ret).__name__,
      (np.asarray(ret).shape if ret is not None else None))
if ret is not None:
    emb = ret
else:
    for getter in ("get_embedding", "embedding"):
        try:
            v = getattr(ex, getter, None) or getattr(stream, getter)
            emb = v(stream) if callable(v) else v
            print(f"  {getter} -> OK")
            break
        except Exception as e:  # noqa: BLE001
            print(f"  {getter} 失败: {e}")

if emb is not None:
    arr = np.asarray(emb, dtype=np.float32).ravel()
    print(f"=== embedding shape={arr.shape} norm={np.linalg.norm(arr):.4f} ===")
    print("  前 5 维:", np.round(arr[:5], 4))
