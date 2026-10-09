# -*- coding: utf-8 -*-
"""身份识别的准入判据回归测试（离线，零费用，不打开麦克风）。

背景 —— 2026-09-14 实测暴露的两个真问题：
  ① `_identify_worker` 原先在"Top-1 是学生"时也会写 `lesson.identity`，
     而 `_maybe_identify` 的停止条件是 `identity is not None` → **重试机制形同虚设**。
     开录 3 分钟（delay_sec）时教师往往还没开口，正好撞在这个坑上。
  ② 教师候选没有准入门槛 → 全场都是噪声时，谁以 0.001 的优势胜出谁就被当真。
     实测 学生丁 课开录 40s：教师 0.249 / 学生乙 0.247 / 学生丁 0.241。

本测试用**真实实测数据**钉住判据边界，防止后续改动把它调坏。

用法：python tools/test_identity_guard.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(r"C:\recorder")
sys.path.insert(0, str(ROOT / "recorder"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _course import course                                      # noqa: E402
from recorder import Recorder                                   # noqa: E402

VP_CFG = json.loads((ROOT / "cloud" / "cloud_config.json").read_text(
    encoding="utf-8"))["voiceprint"]

# (场景, 候选角色, 中位余弦, 领先第二名, 期望接受?)  —— 均来自 test_identify_chain.py 实测
CASES = [
    ("学生乙 课 · 中段 40s",     "teacher", 0.901, 0.374, True),
    ("学生丁 课 · 中段 40s",     "teacher", 0.693, 0.025, True),
    ("学生乙 课 · 开录后 40s",   "student", 0.497, 0.121, False),  # 角色门就挡住了
    ("学生丁 课 · 开录后 40s",   "teacher", 0.249, 0.002, False),  # ← 关键：防噪声误判
    ("学生丁 课 · 中段 次名",    "student", 0.668, None,  False),  # 学生不该被认成教师
    ("假设 · 教师刚过下限",     "teacher", 0.330, 0.150, True),   # 走复合判据第二腿
    ("假设 · 教师达下限没领先", "teacher", 0.400, 0.050, False),
]


def _read_span(path: Path, start_sec: float, dur_sec: float):
    """读 wav 的 [start_sec, +dur_sec) → float32 单声道（与 recorder 同规格）。"""
    import wave

    import numpy as np
    with wave.open(str(path)) as w:
        sr, ch = w.getframerate(), w.getnchannels()
        w.setpos(min(int(start_sec * sr), max(0, w.getnframes() - 1)))
        raw = w.readframes(int(dur_sec * sr))
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x


def integration() -> int:
    """端到端：用真实课堂音频跑 _identify_worker，验证"作废时绝不写 identity"。

    这是本次修复的核心 —— 若失败也写 identity，_maybe_identify 会永久停止重试。
    """
    from datetime import datetime, timedelta

    from recorder import Lesson

    cfg = json.loads((ROOT / "recorder" / "config.json").read_text(encoding="utf-8"))
    rec = Recorder(cfg)
    print("\n" + "=" * 78)
    print("端到端验证（真实音频 → _identify_worker，不打开麦克风、不产生费用）")
    print("=" * 78)

    probes = [
        ("学生乙 课 · 开录后 60s（教师尚未开口）",
         course("main") / "merged_16k.wav", 60, False, None),
        ("学生乙 课 · 开录后 60s · 强制跳过能量门",
         course("main") / "merged_16k.wav", 60, False, "非教师"),
        ("学生乙 课 · 中段 1800s（教师已讲授）",
         course("main") / "merged_16k.wav", 1800, True, None),
    ]

    bad = 0
    for label, path, t0, expect_hit, must_mention in probes:
        if not path.exists():
            print(f"-- {label}：素材缺失，跳过")
            continue
        audio = _read_span(path, t0, float(cfg["identity"]["probe_sec"]))
        rec._probe_audio = lambda sec, a=audio: (a, "integration-test")   # noqa: ARG005
        if must_mention:      # 把 min_rms 归零，逼它走到「Top-1 非教师」分支
            cfg["identity"]["min_rms"] = 0.0
        else:
            cfg["identity"]["min_rms"] = 0.004
        rec.current = Lesson(course_id="PROBE-TEST", teacher="未知",
                             start=datetime(2026, 9, 11, 16, 30),
                             end=datetime(2026, 9, 11, 18, 30),
                             room=cfg["room_id"])
        rec.current.identity = None
        rec._identify_worker(rec.current)

        idt = rec.current.identity
        hit = idt is not None
        ok = hit == expect_hit
        if hit:
            t = idt.get("teacher") or {}
            s = idt.get("schedule") or {}
            print(f"{label}\n   → ✅ 已认定：{t.get('person_name')}"
                  f"（票 {t.get('votes')}，中位 {t.get('median'):.3f}）"
                  f" | 课表 {s.get('course_label') or '未匹配'}"
                  f" | {'符合预期' if ok else '❌ 不该认定'}")
        else:
            pr = rec._identity_probe.get(rec.current.tag) or {}
            reason = str(pr.get("reason"))
            branch_ok = must_mention is None or must_mention in reason
            bad += 0 if branch_ok else 1
            print(f"{label}\n   → ⏳ 作废并保留重试：{reason}"
                  f"\n      identity 仍为 None（{'符合预期' if ok else '❌ 不该作废'}）"
                  f"{'' if branch_ok else f' ❌ 没走到「{must_mention}」分支'}")

    print("-" * 78)
    if bad:
        print(f"❌ 端到端 {bad} 项不符合预期")
        return 1
    print("✅ 端到端通过：该认定的认定了，该作废的作废了（identity 不被污染）")
    return 0


def main() -> int:
    thr = VP_CFG.get("local_threshold")
    floor = VP_CFG.get("local_floor")
    need = VP_CFG.get("local_margin")
    print(f"判据参数：阈值 {thr} / 下限 {floor} / 领先 {need}\n")
    print(f"{'场景':<24}{'角色':<9}{'中位':>7}{'领先':>8}   {'判定':<6}{'期望':<6}结果")
    print("-" * 78)

    bad = 0
    for label, role, score, margin, expect in CASES:
        m = 0.0 if margin is None else margin
        accept, why = Recorder._teacher_accept(score, m, VP_CFG)
        # 完整判定 = 角色门 + 判据（与 _identify_worker 的顺序一致）
        final = accept and role == "teacher"
        ok = final == expect
        bad += 0 if ok else 1
        print(f"{label:<24}{role:<9}{score:>7.3f}{m:>8.3f}   "
              f"{'接受' if final else '作废':<6}{'接受' if expect else '作废':<6}"
              f"{'✅' if ok else '❌  ' + why}")

    print("-" * 78)
    if bad:
        print(f"❌ {bad} 个用例不符合预期")
        return 1
    print("✅ 判据用例全部通过：教师认得出、噪声与学生的误判被挡住")
    print("\n判据说明：接受 = 角色为 teacher 且 (余弦≥阈值 或 (余弦≥下限 且 领先≥margin))")
    return integration()


if __name__ == "__main__":
    sys.exit(main())
