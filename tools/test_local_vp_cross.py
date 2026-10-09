# -*- coding: utf-8 -*-
"""本地声纹跨课程验证：用 A 课的教师片段注册，去验证 B 课的教师片段。

意义：教师片段取自「远场教室麦」，与学生「近场耳麦」是两个声学域。
若跨课程仍能正确命中，说明本地模型可用；同时暴露真实阈值应在什么区间。
"""
import sys
from pathlib import Path

sys.path.insert(0, r"C:\recorder\cloud")
from voiceprint_local import LocalVoiceprint  # noqa: E402

DB = r"C:\recorder\cloud\voiceprint_local_db.json"
LUCAS = Path(r"C:\recorder\cloud_work\Lucas-Zhang-IG物理")
GAVIN = Path(r"C:\recorder\cloud_work\Gavin-IG物理")

db = LocalVoiceprint(db_path=DB)

print("=== 当前库 ===")
for r in db.list_persons():
    print(f"  {r['person_id']:6s} {r['person_name']:14s} {r['role']:8s} {r['samples']} 段")

print("\n=== 用 Lucas 课的教师片段注册 汤朔 ===")
r = db.enroll("T001", "汤朔", "teacher", [str(LUCAS / "vp_0_0.wav")])
print(f"  +{r['added']} 段，累计 {r['total_embeddings']} 段")

print("\n=== 跨课程验证：Gavin 课的教师片段（同一人，不同课/不同日期）===")
for f in ["vp_0.wav", "vp_0_0.wav", "vp_0_1.wav"]:
    p = GAVIN / f
    if not p.exists():
        continue
    print(f"  {f}:")
    for t in db.verify_topk(str(p), topk=4):
        mark = " ← 命中教师" if t["person_id"] == "T001" else ""
        print(f"     {t['person_name']:14s} cos={t['cosine']:.3f}  score={t['score']:5.1f}{mark}")

print("\n=== 反向检查：学生片段不该被老师抢走 ===")
for label, p, expect in [
    ("Lucas 片段", LUCAS / "vp_1_1.wav", "S002"),
    ("Gavin 片段", GAVIN / "vp_1_1.wav", "S004"),
]:
    if not p.exists():
        continue
    tops = db.verify_topk(str(p), topk=3)
    ok = tops and tops[0]["person_id"] == expect
    print(f"  {'✅' if ok else '❌'} {label}: " +
          " | ".join(f"{t['person_name']}={t['cosine']:.3f}" for t in tops))

print("\n=== 域失配量化：同一人在不同声学域下的余弦 ===")
same = db.embed_wav(str(LUCAS / "vp_0_0.wav")) @ db.embed_wav(str(GAVIN / "vp_0.wav"))
diff = db.embed_wav(str(GAVIN / "vp_0.wav")) @ db.embed_wav(str(GAVIN / "vp_1_1.wav"))
print(f"  同人·跨课（老师 vs 老师）        = {same:.3f}")
print(f"  异人·同课（老师 vs 学生）        = {diff:.3f}")
print(f"  → 判别间隔 {same - diff:+.3f}")
