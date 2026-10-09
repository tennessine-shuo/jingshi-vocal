# -*- coding: utf-8 -*-
"""本地声纹跨课程验证：用 A 课的教师片段注册，去验证 B 课的教师片段。

意义：教师片段取自「远场教室麦」，与学生「近场耳麦」是两个声学域。
若跨课程仍能正确命中，说明本地模型可用；同时暴露真实阈值应在什么区间。
"""
import sys
from pathlib import Path

sys.path.insert(0, r"C:\recorder\cloud")
from _course import course, person  # noqa: E402
from voiceprint_local import LocalVoiceprint  # noqa: E402

DB = r"C:\recorder\cloud\voiceprint_local_db.json"
COURSE_A = course("main")
COURSE_B = course("cross")

db = LocalVoiceprint(db_path=DB)

print("=== 当前库 ===")
for r in db.list_persons():
    print(f"  {r['person_id']:6s} {r['person_name']:14s} {r['role']:8s} {r['samples']} 段")

print("\n=== 用 学生乙 课的教师片段注册 教师 ===")
r = db.enroll("T001", person("teacher"), "teacher", [str(COURSE_A / "vp_0_0.wav")])
print(f"  +{r['added']} 段，累计 {r['total_embeddings']} 段")

print("\n=== 跨课程验证：学生丁 课的教师片段（同一人，不同课/不同日期）===")
for f in ["vp_0.wav", "vp_0_0.wav", "vp_0_1.wav"]:
    p = COURSE_B / f
    if not p.exists():
        continue
    print(f"  {f}:")
    for t in db.verify_topk(str(p), topk=4):
        mark = " ← 命中教师" if t["person_id"] == "T001" else ""
        print(f"     {t['person_name']:14s} cos={t['cosine']:.3f}  score={t['score']:5.1f}{mark}")

print("\n=== 反向检查：学生片段不该被老师抢走 ===")
for label, p, expect in [
    ("学生乙 片段", COURSE_A / "vp_1_1.wav", "S002"),
    ("学生丁 片段", COURSE_B / "vp_1_1.wav", "S004"),
]:
    if not p.exists():
        continue
    tops = db.verify_topk(str(p), topk=3)
    ok = tops and tops[0]["person_id"] == expect
    print(f"  {'✅' if ok else '❌'} {label}: " +
          " | ".join(f"{t['person_name']}={t['cosine']:.3f}" for t in tops))

print("\n=== 域失配量化：同一人在不同声学域下的余弦 ===")
same = db.embed_wav(str(COURSE_A / "vp_0_0.wav")) @ db.embed_wav(str(COURSE_B / "vp_0.wav"))
diff = db.embed_wav(str(COURSE_B / "vp_0.wav")) @ db.embed_wav(str(COURSE_B / "vp_1_1.wav"))
print(f"  同人·跨课（老师 vs 老师）        = {same:.3f}")
print(f"  异人·同课（老师 vs 学生）        = {diff:.3f}")
print(f"  → 判别间隔 {same - diff:+.3f}")
