# -*- coding: utf-8 -*-
"""集成验证：直接调用 stages._voiceprint_local_match，走完整生产代码路径。

用 Lucas 课的本地缓存（merged_16k.wav + asr_cache.json），零 ASR 费用。
"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, r"C:\recorder\cloud")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

import stages  # noqa: E402

WD = Path(r"C:\recorder\cloud_work\Lucas-Zhang-IG物理")
cfg = json.loads(Path(r"C:\recorder\cloud\cloud_config.json").read_text(encoding="utf-8"))

sentences = json.loads((WD / "asr_cache.json").read_text(encoding="utf-8"))
print(f"ASR 句数 {len(sentences)}")

# 复刻 diarize：合并同人相邻句
timeline: list[dict] = []
for s in sentences:
    if timeline and timeline[-1]["speaker_id"] == s["speaker_id"] and \
            s["start_sec"] - timeline[-1]["end_sec"] < 1.0:
        timeline[-1]["end_sec"] = s["end_sec"]
    else:
        timeline.append({"speaker_id": s["speaker_id"],
                         "start_sec": s["start_sec"], "end_sec": s["end_sec"]})
print(f"时间轴 {len(timeline)} 条")

ctx = {"config": cfg, "workdir": str(WD), "merged_wav": str(WD / "merged_16k.wav"), "_asr": sentences}
meta = {"course_id": "Lucas-Zhang-IG物理", "start": "2026-09-11T16:30:00", "end": "2026-09-11T18:30:00"}

print("\n=== 调用 stages._voiceprint_local_match（生产代码路径）===")
res = stages._voiceprint_local_match(timeline, sentences, cfg["voiceprint"], meta, ctx)
for r in res:
    print(f"  speaker {r['speaker_id']}: {r['role']:8s} {r['person_name']:14s} "
          f"conf={r['confidence']}  method={r['method']}  "
          f"score={r.get('match_score')}  margin={r.get('match_margin')}")

print("\n=== 对照：同一片段走云端引擎（腾讯云实际分数）===")
try:
    cloud = stages._voiceprint_api_match(timeline, sentences,
                                         stages._load_registry(cfg["voiceprint"], ctx),
                                         cfg["voiceprint"], meta, ctx)
    for r in cloud:
        print(f"  speaker {r['speaker_id']}: {r['role']:8s} {r['person_name']:14s} "
              f"conf={r['confidence']}  method={r['method']}  "
              f"score={r.get('match_score')}  margin={r.get('match_margin')}")
except Exception as e:  # noqa: BLE001
    print("  云端对照失败（不影响本地结论）：", e)
