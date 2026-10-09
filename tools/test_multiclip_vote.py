# -*- coding: utf-8 -*-
"""验证「多片段分别取 embedding 再投票」在 pipeline 路径上的效果。

用 Lucas 课缓存（asr_cache.json 的句级 speaker_id + 本地 merged_16k.wav）复现
stages._voiceprint_local_match，并对比：
  A. 旧行为：把若干句拼接成一条 ≤25s 音频 → 单次比对
  B. 新行为：取 N 条独立片段 → 各自比对 → 票数聚合

零 API 费用（走 local 引擎 + 本地音频）。

用法：python tools/test_multiclip_vote.py
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(r"C:\recorder")
sys.path.insert(0, str(ROOT / "cloud"))
sys.path.insert(0, str(ROOT / "recorder"))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

import stages                                        # noqa: E402
from voiceprint_local import LocalVoiceprint         # noqa: E402

WD = ROOT / "cloud_work" / "Lucas-Zhang-IG物理"


def main() -> int:
    cfg = json.loads((ROOT / "cloud" / "cloud_config.json").read_text(encoding="utf-8"))
    ctx = {"config": cfg, "workdir": str(WD), "merged_wav": str(WD / "merged_16k.wav")}
    sentences = json.loads((WD / "asr_cache.json").read_text(encoding="utf-8"))
    print(f"ASR 句数：{len(sentences)}")
    from collections import Counter
    print("说话人分布：", dict(Counter(s["speaker_id"] for s in sentences)))

    speakers = [{"speaker_id": sid} for sid in sorted({s["speaker_id"] for s in sentences})]
    vp_cfg = dict(cfg["voiceprint"])
    vp_cfg["engine"] = "local"

    print("\n" + "=" * 74)
    print(f"A. 旧行为：拼接单片段（_extract_speaker_clip，max 25s）")
    print("=" * 74)
    ffmpeg = stages._find_ffmpeg()
    db = LocalVoiceprint(model_path=vp_cfg["local_model"], db_path=vp_cfg["local_db"])
    for sp in speakers:
        clip = stages._extract_speaker_clip(ffmpeg, ctx["merged_wav"],
                                            sp["speaker_id"], sentences,
                                            str(WD), max_sec=20)
        if not clip:
            print(f"  speaker {sp['speaker_id']}: 无音频")
            continue
        dur = Path(clip).stat().st_size / 32000
        tops = db.verify_topk(clip, topk=3)
        print(f"  speaker {sp['speaker_id']}: 拼接 {Path(clip).name} ({dur:.1f}s) → "
              + " | ".join(f"{t['person_name']}={t['cosine']:.3f}" for t in tops))

    print("\n" + "=" * 74)
    print(f"B. 新行为：{vp_cfg.get('vote_clips', 3)} 条独立片段 → 投票")
    print("=" * 74)
    for sp in speakers:
        clips = stages._extract_speaker_clips(ffmpeg, ctx["merged_wav"],
                                              sp["speaker_id"], sentences,
                                              str(WD), n_clips=int(vp_cfg.get("vote_clips", 3)))
        durs = [round(Path(c).stat().st_size / 32000, 1) for c in clips]
        res = db.verify_multi_sources(clips, topk=3)
        print(f"  speaker {sp['speaker_id']}: {len(clips)} 片 时长{durs}s")
        for t in res.get("tops", []):
            print(f"      {t['person_name']:14s} 票{t['votes']} "
                  f"中位{t['median']:.3f} max{t['max']:.3f} min{t['min']:.3f}")
        print(f"      margin={res.get('margin')}")

    print("\n" + "=" * 74)
    print("C. 走完整 pipeline 函数 _voiceprint_local_match")
    print("=" * 74)
    out = stages._voiceprint_local_match(speakers, sentences, vp_cfg,
                                         {"course_id": "Lucas-Zhang-IG物理"}, ctx)
    for r in out:
        print(f"  speaker {r['speaker_id']} → {r['role']:8s} {r['person_name']:14s} "
              f"method={r['method']:22s} score={r.get('match_score')} "
              f"votes={r.get('match_votes')}")

    print("\n" + "=" * 74)
    print(f"D. 云端引擎路径 _voiceprint_api_match（真实 API，约 "
          f"{len(speakers) * int(cfg['voiceprint'].get('vote_clips', 3)) * 0.0042:.3f} 元）")
    print("=" * 74)
    vp_cloud = dict(cfg["voiceprint"])
    registry = stages._load_registry(vp_cloud, ctx)
    print(f"  注册库：{len(registry)} 条声纹")
    out2 = stages._voiceprint_api_match(speakers, sentences, registry, vp_cloud,
                                        {"course_id": "Lucas-Zhang-IG物理"}, ctx)
    for r in out2:
        print(f"  speaker {r['speaker_id']} → {r['role']:8s} {r['person_name']:14s} "
              f"method={r['method']:16s} score={r.get('match_score')} "
              f"margin={r.get('match_margin')} votes={r.get('match_votes')}")

    same = all(a["person_id"] == b["person_id"] for a, b in zip(out, out2))
    print(f"\n  本地 vs 云端结论：{'✅ 完全一致' if same else '⚠️ 不一致，需复核'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
