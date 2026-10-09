# -*- coding: utf-8 -*-
"""端到端验证：录音启动后的身份识别链路（本地声纹 → 飞书课表）。

模拟 recorder._identify_worker 的真实流程：
  取开录后一段课堂音频 → 切 N 段投票 → 认出教师 → 用教师姓名反查飞书课表。
全程只读本地录音，不打开麦克风，不产生任何云端费用。

用法：python tools/test_identify_chain.py
"""
from __future__ import annotations

import json
import sys
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(r"C:\recorder")
sys.path.insert(0, str(ROOT / "cloud"))
sys.path.insert(0, str(ROOT / "recorder"))

from voiceprint_local import LocalVoiceprint          # noqa: E402


def read_span(path: Path, start_sec: float, dur_sec: float) -> np.ndarray:
    """读 wav 的 [start_sec, start_sec+dur_sec) → float32 16k 单声道。"""
    with wave.open(str(path)) as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        n = w.getnframes()
        if sw != 2:
            raise ValueError(f"仅支持 16bit：{path}")
        w.setpos(min(int(start_sec * sr), max(0, n - 1)))
        raw = w.readframes(int(dur_sec * sr))
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x


def fmt(res: dict) -> str:
    if not res.get("hit"):
        return f"无结果({res.get('reason')})"
    return " | ".join(
        f"{t['person_name']}/{t['role'][:4]} 票{t.get('votes', '-')} "
        f"中位{t['median']:.3f}" for t in res["tops"])


def main() -> int:
    cfg = json.loads((ROOT / "recorder" / "config.json").read_text(encoding="utf-8"))
    icfg = cfg["identity"]
    db = LocalVoiceprint(model_path=icfg["model"], db_path=icfg["db"])
    print(f"本地声纹库：{len(db.persons)} 人 "
          f"{[r['person_name'] for r in db.persons.values()]}")
    print(f"投票片段数：{icfg['vote_clips']}，探测时长：{icfg['probe_sec']}s\n")

    cases = [
        ("Lucas 课 · 开录后 40s",ROOT / "cloud_work/Lucas-Zhang-IG物理/merged_16k.wav", 60, "汤朔"),
        ("Lucas 课 · 中段 40s", ROOT / "cloud_work/Lucas-Zhang-IG物理/merged_16k.wav", 1800, "汤朔"),
        ("Gavin 课 · 开录后 40s", ROOT / "cloud_work/Gavin-IG物理/merged_16k.wav", 60, "汤朔"),
        ("Gavin 课 · 中段 40s", ROOT / "cloud_work/Gavin-IG物理/merged_16k.wav", 1500, "汤朔"),
    ]

    for label, path, t0, expect in cases:
        if not path.exists():
            print(f"-- {label}：素材缺失，跳过\n")
            continue
        audio = read_span(path, t0, float(icfg["probe_sec"]))
        n_max = max(1, len(audio) // (16000 * 3))
        clips = [a for a in np.array_split(audio, min(int(icfg["vote_clips"]), n_max))
                 if len(a) > 16000]

        single = db.verify_multi_sources([audio], topk=3)
        voted = db.verify_multi_sources(clips, topk=3)

        print(f"### {label}   期望教师={expect}")
        print(f"  单段(拼接整条): {fmt(single)}")
        print(f"    margin={single.get('margin')}")
        print(f"  多段投票({len(clips)}段): {fmt(voted)}")
        print(f"    margin={voted.get('margin')}  clips={voted.get('clips')}")
        t1 = (voted.get("tops") or [{}])[0]
        ok = t1.get("person_name") == expect and t1.get("role") == "teacher"
        print(f"  → {'✅ 命中教师' if ok else '❌ 未命中'}\n")

    # ---- 课表反查：走 recorder 的真实方法 ----
    print("### 课表反查（Recorder._match_lesson_online）")
    from recorder import Lesson, Recorder                      # noqa: E402
    rec = Recorder(cfg)
    probes = [
        ("汤朔", datetime(2026, 9, 11, 16, 30), datetime(2026, 9, 11, 18, 30)),
        ("汤朔", datetime(2026, 9, 11, 19, 0), datetime(2026, 9, 11, 21, 0)),
    ]
    for teacher, st, en in probes:
        lesson = Lesson(course_id="PROBE", teacher=teacher, start=st, end=en,
                        room=cfg["room_id"])
        try:
            r = rec._match_lesson_online(teacher, lesson)
        except Exception as e:                                 # noqa: BLE001
            print(f"  ⚠️ {st:%m-%d %H:%M} 查询异常：{e}")
            continue
        if r:
            print(f"  ✅ {st:%m-%d %H:%M} 教师={teacher} → {r.get('course_label')} / "
                  f"学生={r.get('student_name')} / {r.get('course_type')}")
        else:
            print(f"  ❌ {st:%m-%d %H:%M} 教师={teacher} → 未匹配")
    return 0


if __name__ == "__main__":
    sys.exit(main())
