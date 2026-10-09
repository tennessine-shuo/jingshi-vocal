# -*- coding: utf-8 -*-
"""用「课堂片段」重建本地声纹库（同域注册），并做留出验证。

为什么必须同域：本地 CAM++ 模型的余弦相似度对声学域极其敏感。
  近场耳麦注册 + 远场教室比对 → 同人余弦从 0.8 掉到 0.3，区分度崩塌。
  注册与比对都用课堂片段，余弦才回到 0.7 量级。

注册素材来自历史课程里**腾讯云已确认身份**的说话人片段（冷启动路径），
之后可逐步补充更多课程样本。

用法：python build_local_vp_db.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, r"C:\recorder\cloud")
from _course import course, enroll_sample, person  # noqa: E402
from voiceprint_local import LocalVoiceprint  # noqa: E402

DB = r"C:\recorder\cloud\voiceprint_local_db.json"
COURSE_A = course("main")
COURSE_B = course("cross")

# 注册集（同域课堂片段）
ENROLL = [
    ("T001", person("teacher"), "teacher",
     [COURSE_A / "vp_0_0.wav", COURSE_B / "vp_0_0.wav"]),
    ("S002", person("S002"), "student", [COURSE_A / "vp_1_1.wav"]),
    ("S004", person("S004"), "student", [COURSE_B / "vp_1_1.wav"]),
]

# 留出验证集（同课但不同片段，考查泛化）
PROBES = [
    (COURSE_B / "vp_0_1.wav", "T001", "学生丁课·教师片段B"),
    (COURSE_B / "vp_1_0.wav", "S004", "学生丁课·学生片段B"),
    (COURSE_B / "vp_1_2.wav", "S004", "学生丁课·学生片段C"),
    (COURSE_A / "vp_1.wav", "S002", "学生乙课·学生片段B"),
    (COURSE_A / "vp_0_0.wav", "T001", "学生乙课·教师片段(注册集内)"),
]


def main() -> int:
    Path(DB).unlink(missing_ok=True)          # 干净重建
    db = LocalVoiceprint(db_path=DB)

    print("=== 1) 同域注册（课堂片段）===")
    for pid, name, role, wavs in ENROLL:
        wavs = [str(w) for w in wavs if w.exists()]
        if not wavs:
            print(f"  {name}: 无可用素材，跳过")
            continue
        r = db.enroll(pid, name, role, wavs)
        print(f"  {pid:6s} {name:14s} {role:8s} +{r['added']} 段")

    print("\n=== 2) 留出验证（同课不同片段）===")
    ok_n = 0
    for wav, expect, label in PROBES:
        if not wav.exists():
            print(f"  -- {label}: 素材缺失，跳过")
            continue
        tops = db.verify_topk(str(wav), topk=3)
        hit = bool(tops) and tops[0]["person_id"] == expect
        ok_n += hit
        line = " | ".join(f"{t['person_name']}={t['cosine']:.3f}" for t in tops)
        margin = (tops[0]["cosine"] - tops[1]["cosine"]) if len(tops) > 1 else 0
        print(f"  {'✅' if hit else '❌'} {label:24s} {line}   领先 {margin:+.3f}")
    print(f"\n  命中 {ok_n} / {len(PROBES)}")

    print("\n=== 3) 误识别压力测试：陌生人应低分 ===")
    stranger = enroll_sample("S003_*.wav")
    if stranger and stranger.exists():
        tops = db.verify_topk(str(stranger), topk=3)
        print("  近场陌生人(不在库中) → " +
              " | ".join(f"{t['person_name']}={t['cosine']:.3f}" for t in tops))

    print("\n=== 4) 最终库 ===")
    for r in db.list_persons():
        print(f"  {r['person_id']:6s} {r['person_name']:14s} {r['role']:8s} {r['samples']} 段")
    return 0


if __name__ == "__main__":
    sys.exit(main())
