# -*- coding: utf-8 -*-
"""本地声纹阈值标定 —— 从历史课程自动挖掘已标注片段，清洗标注噪声后推荐阈值。

为什么不用人工标注：
  历史课程产物（reports/<date>/*_transcript.json）里已有**逐句 speaker_id 与
  时间轴**，配合本地留存的 merged_16k.wav，可零成本复现上千条带标签片段。

⚠️ 但产物里的 role/person_name 本身可能是错的：
   云端声纹失败时会降级为启发式（method=heuristic / voiceprint_miss），
   产物里就会出现"最长的说话人被标成 teacher（名字=未知）"这类**噪声标签**。
   直接用它们标定会得到"同人 P5 < 异人 P95"的荒谬结论。
   因此本工具先用**自洽性**给每组 (课程, speaker_id) 推定身份并算纯度，
   只采信高纯度组 —— 这本身就是多片段投票思想在标定上的应用。

产出：
  1) 标注质量诊断：每组推定身份 + 纯度（自动暴露错标）
  2) 同人 / 异人余弦分布（仅高纯度组）
  3) 阈值扫描（单阈值 与 复合判据）的命中率 / 误报率
  4) 录音时点扫描：开录后第 T 秒取窗口的教师识别命中率 → 定 delay_sec
  5) 推荐配置

用法：
  python tools/calibrate_local_vp.py --max-per-speaker 120
  python tools/calibrate_local_vp.py --purity 0.7 --min-group 15
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(r"C:\recorder")
sys.path.insert(0, str(ROOT / "cloud"))
sys.path.insert(0, str(ROOT / "recorder"))

from voiceprint_local import LocalVoiceprint          # noqa: E402


# ---------------------------------------------------------------- 素材

def find_courses() -> list[dict]:
    """找出"有产物 segments + 有本地合并音频"的课程。"""
    found: dict[str, dict] = {}
    for prod in sorted(ROOT.glob("reports/*/*.json")):
        if not prod.name.endswith(("_transcript.json", "_report.json")):
            continue
        try:
            d = json.loads(prod.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        segs = d.get("segments") or []
        if not segs:
            continue
        cid = (d.get("meta") or {}).get("course_id") or ""
        merged = ROOT / "cloud_work" / cid / "merged_16k.wav"
        if cid and merged.exists() and cid not in found:
            found[cid] = {"course_id": cid, "merged": merged, "segments": segs,
                          "source": prod.name}
    return list(found.values())


class MergedAudio:
    """按需切合并音频片段（直接 seek，不经过 ffmpeg）。"""

    def __init__(self, path: Path):
        with wave.open(str(path)) as w:
            self.sr, self.ch, self.frames = (w.getframerate(), w.getnchannels(),
                                             w.getnframes())
        self.path = str(path)
        self.dur = self.frames / max(1, self.sr)

    def span(self, start_sec: float, dur_sec: float) -> np.ndarray | None:
        if dur_sec <= 0 or start_sec < 0:
            return None
        with wave.open(self.path) as w:
            w.setpos(min(int(start_sec * self.sr), max(0, self.frames - 1)))
            raw = w.readframes(int(dur_sec * self.sr))
        if len(raw) < self.sr // 2:                  # <0.5 秒视为无效
            return None
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if self.ch > 1:
            x = x.reshape(-1, self.ch).mean(axis=1)
        return x


def speaker_roles(segments: list[dict]) -> dict[str, str]:
    """产物里每个 speaker_id 的众数角色（仅作参考，可能不可靠）。"""
    cnt: dict[str, Counter] = defaultdict(Counter)
    for s in segments:
        cnt[s["speaker_id"]][s.get("role") or "?"] += 1
    return {sid: c.most_common(1)[0][0] for sid, c in cnt.items()}


# ---------------------------------------------------------------- 统计小工具

def pct(vals: list[float], q: float) -> float:
    return float(np.percentile(vals, q)) if vals else float("nan")


def describe(vals: list[float]) -> str:
    if not vals:
        return "（无样本）"
    return (f"n={len(vals):4d}  中位 {np.median(vals):.3f}  "
            f"P5 {pct(vals, 5):.3f}  P25 {pct(vals, 25):.3f}  "
            f"P75 {pct(vals, 75):.3f}  P95 {pct(vals, 95):.3f}  "
            f"min {min(vals):.3f}  max {max(vals):.3f}")


def fnum(x: float | None, nd: int = 3) -> str:
    return "  --  " if x is None or (isinstance(x, float) and np.isnan(x)) \
        else f"{x:.{nd}f}"


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="本地声纹阈值标定")
    ap.add_argument("--model", default=r"C:\recorder\models\campplus_sv_zh_16k.onnx")
    ap.add_argument("--db", default=r"C:\recorder\cloud\voiceprint_local_db.json")
    ap.add_argument("--max-per-speaker", type=int, default=150)
    ap.add_argument("--min-seg-sec", type=float, default=1.5)
    ap.add_argument("--purity", type=float, default=0.6,
                    help="组内 Top-1 集中在同一人的最低占比，低于此判为不可采信")
    ap.add_argument("--min-group", type=int, default=12,
                    help="样本数少于此的组不参与标定")
    ap.add_argument("--out", default=r"C:\recorder\reports\声纹标定报告.json")
    a = ap.parse_args()

    db = LocalVoiceprint(model_path=a.model, db_path=a.db)
    print("=" * 82)
    print("本地声纹阈值标定")
    print("=" * 82)
    print("声纹库：")
    for r in db.list_persons():
        print(f"   {r['person_id']:6s} {r['person_name']:16s} {r['role']:8s} "
              f"{r['samples']} 段样本")
    print(f"采样上限 {a.max_per_speaker} 句/人，最短句 {a.min_seg_sec}s，"
          f"纯度阈值 {a.purity}，最小组样本 {a.min_group}\n")

    courses = find_courses()
    if not courses:
        print("❌ 未找到可标定课程（需同时具备 产物segments + cloud_work/*/merged_16k.wav）")
        return 1
    print(f"发现 {len(courses)} 门课程：")
    for c in courses:
        print(f"   · {c['course_id']:32s} {len(c['segments']):5d} 句  "
              f"音频 {MergedAudio(c['merged']).dur / 60:6.1f} 分钟")
    print()

    # ---- 逐句采样 + 比对 ----
    records: list[dict] = []
    for c in courses:
        audio = MergedAudio(c["merged"])
        roles = speaker_roles(c["segments"])
        by_spk: dict[str, list[dict]] = defaultdict(list)
        for s in c["segments"]:
            if (s["end_sec"] - s["start_sec"]) >= a.min_seg_sec:
                by_spk[s["speaker_id"]].append(s)

        for sid, segs in sorted(by_spk.items()):
            segs.sort(key=lambda s: s["start_sec"])
            step = max(1, len(segs) // a.max_per_speaker)
            for s in segs[::step][:a.max_per_speaker]:
                x = audio.span(s["start_sec"], s["end_sec"] - s["start_sec"])
                if x is None:
                    continue
                try:
                    q = db.embed_array(x, tag=f"{c['course_id']}#{sid}")
                except Exception:  # noqa: BLE001
                    continue
                tops = db.rank_embedding(q, topk=3)
                if not tops:
                    continue
                records.append({
                    "course": c["course_id"], "speaker_id": sid,
                    "declared_role": roles.get(sid, "?"),
                    "start_sec": round(s["start_sec"], 1),
                    "dur": round(s["end_sec"] - s["start_sec"], 2),
                    "tops": tops,
                })
    print(f"共 {len(records)} 条片段样本\n")

    # ---- ① 标注质量诊断（自洽性）----
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in records:
        groups[(r["course"], r["speaker_id"])].append(r)

    print("─" * 82)
    print("① 标注质量诊断：每组按『片段 Top-1 投票』推定身份并算纯度")
    print("   （产物里的 role 只是声明值，可能是声纹失败后的降级噪声）")
    print("─" * 82)
    print(f"   {'课程':26s} {'spk':4s} {'样本':5s} {'声明角色':9s} "
          f"{'推定身份':16s} {'纯度':6s} 采信")
    trusted: dict[tuple[str, str], str] = {}       # (course, spk) → 推定 person_id
    for key, items in sorted(groups.items()):
        votes = Counter(t["tops"][0]["person_id"] for t in items if t["tops"])
        if not votes:
            continue
        winner, n_win = votes.most_common(1)[0]
        purity = n_win / len(items)
        name = db.persons[winner]["person_name"] if winner in db.persons else winner
        ok = purity >= a.purity and len(items) >= a.min_group
        if ok:
            trusted[key] = winner
        course, sid = key
        print(f"   {str(course)[:26]:26s} {str(sid):4s} {len(items):5d} "
              f"{str(items[0]['declared_role']):9s} {str(name)[:16]:16s} "
              f"{purity * 100:5.1f}% {'✅' if ok else '❌'}")

    # ---- ② 同人 / 异人分布（仅采信组）----
    same: list[float] = []
    diff: list[float] = []
    hit_same = 0
    for key, winner in trusted.items():
        for r in groups[key]:
            cos = {t["person_id"]: t["cosine"] for t in r["tops"]}
            if winner in cos:
                same.append(cos[winner])
                if r["tops"][0]["person_id"] == winner:
                    hit_same += 1
            others = [c for p, c in cos.items() if p != winner]
            if others:
                diff.append(max(others))

    print("\n" + "─" * 82)
    print(f"② 同人 / 异人余弦分布（仅 {len(trusted)} 个高纯度组）")
    print("─" * 82)
    print(f"  同人：{describe(same)}")
    print(f"  异人：{describe(diff)}")
    sep = None
    if same and diff:
        sep = pct(same, 5) - pct(diff, 95)
        print(f"  同人 P5 {pct(same, 5):.3f}  vs  异人 P95 {pct(diff, 95):.3f}"
              f"   → 分离度 {sep:+.3f} {'✅' if sep > 0 else '⚠️ 重叠'}")
        print(f"  同人 Top-1 命中率：{hit_same}/{len(same)} "
              f"= {hit_same / max(1, len(same)) * 100:.1f}%")

    # ---- ③ 阈值扫描 ----
    # 统一口径：对每个片段，判据决定"系统是否接受本次识别结果"。
    #   覆盖率 = 被接受的片段占比（越高说明系统越"敢于判断"）
    #   准确率 = 被接受的片段里 Top-1 判对的比例（越高说明越可靠）
    # 只有这两个指标同时高，阈值才算可用。
    samples: list[tuple[str, float, float, str]] = []
    for key, winner in trusted.items():
        for r in groups[key]:
            if not r["tops"]:
                continue
            t2c = r["tops"][1]["cosine"] if len(r["tops"]) > 1 else 0.0
            samples.append((r["tops"][0]["person_id"], r["tops"][0]["cosine"],
                            t2c, winner))
    n_same = sum(1 for s in samples if s[0] == s[3])

    def eval_acc(label: str, acc) -> tuple[float, float]:
        accepted = [s for s in samples if acc(s[1], s[2])]
        correct = [s for s in accepted if s[0] == s[3]]
        cov = len(accepted) / max(1, len(samples)) * 100
        prec = len(correct) / max(1, len(accepted)) * 100
        print(f"   {label:28s} 覆盖 {cov:5.1f}%   准确 {prec:5.1f}%"
              f"   （接受 {len(accepted)}/{len(samples)}，判对 {len(correct)}）")
        return cov, prec

    print("\n" + "─" * 82)
    print(f"③ 判据扫描（{len(samples)} 片段，其中 Top-1 正确 {n_same} 条）")
    print("─" * 82)
    print("  单阈值 cos>=T：")
    for T in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
        eval_acc(f"cos >= {T:.2f}", lambda c, c2, T=T: c >= T)
    print("  复合判据 cos>=F 且 top1-top2>=M：")
    for F, M in ((0.30, 0.10), (0.35, 0.10), (0.35, 0.15), (0.40, 0.15),
                 (0.45, 0.15), (0.40, 0.20), (0.50, 0.20)):
        eval_acc(f"cos>={F:.2f} 且 margin>={M:.2f}",
                 lambda c, c2, F=F, M=M: c >= F and (c - c2) >= M)

    # ---- ④ 录音时点扫描 ----
    print("\n" + "─" * 82)
    print("④ 录音时点扫描：开录后第 T 秒取窗口 → 能否认出【库中教师】")
    print("   （这是 recorder.identity.delay_sec 的直接依据）")
    print("─" * 82)
    teacher_pids = {pid for pid, r in db.persons.items() if r["role"] == "teacher"}
    # 只统计"该课确实有一位已注册教师"的课：以采信组里出现 teacher 为标志
    teacher_courses = {k[0] for k, pid in trusted.items() if pid in teacher_pids}
    print(f"    （参与统计的课程：{sorted(teacher_courses) or '无'}）")
    for probe_sec in (30.0, 40.0):
        line = f"    窗口 {probe_sec:.0f}s : "
        for t0 in (30, 60, 90, 120, 180, 240, 300, 600):
            ok = tot = 0
            for cid in teacher_courses:
                c = next(x for x in courses if x["course_id"] == cid)
                audio = MergedAudio(c["merged"])
                if audio.dur < t0 + probe_sec:
                    continue
                x = audio.span(float(t0), probe_sec)
                if x is None:
                    continue
                res = db.verify_multi_sources([x], topk=3)
                if not res.get("hit"):
                    continue
                tot += 1
                t1 = res["tops"][0]
                if t1["person_id"] in teacher_pids and t1["role"] == "teacher":
                    ok += 1
            line += (f"{t0}s:{ok}/{tot}  " if tot else f"{t0}s:--  ")
        print(line)

    # ---- ⑤ 投票片段数扫描 ----
    print("\n" + "─" * 82)
    print("⑤ 探测片段切分数扫描（40s 窗口 → 切 N 段投票）")
    print("─" * 82)
    for n_clip in (1, 2, 3, 4):
        ok = tot = 0
        for cid in teacher_courses:
            c = next(x for x in courses if x["course_id"] == cid)
            audio = MergedAudio(c["merged"])
            for t0 in (120, 180, 240, 300):
                if audio.dur < t0 + 40:
                    continue
                x = audio.span(float(t0), 40.0)
                if x is None:
                    continue
                clips = (x if n_clip == 1 else
                         [s for s in np.array_split(x, n_clip) if len(s) > 16000])
                res = db.verify_multi_sources(
                    clips if isinstance(clips, list) else [clips], topk=3)
                if not res.get("hit"):
                    continue
                tot += 1
                t1 = res["tops"][0]
                if t1["person_id"] in teacher_pids and t1["role"] == "teacher":
                    ok += 1
        if tot:
            print(f"   {n_clip} 段/票：命中 {ok}/{tot} = {ok / tot * 100:5.1f}%")
    print("   （样本课数有限，正式标定需积累更多课程）")

    # ---- ⑥ 推荐 ----
    print("\n" + "─" * 82)
    print("⑥ 推荐配置")
    print("─" * 82)
    rec = {}
    if same and diff:
        rec = {
            "local_threshold": round(max(0.30, pct(same, 5)), 2),
            "local_floor": round(max(0.25, pct(same, 5) - 0.08), 2),
            "local_margin": round(max(0.05, pct(same, 50) - pct(diff, 95)), 2),
        }
    print(f"  cloud_config.json → voiceprint : {json.dumps(rec, ensure_ascii=False)}")
    print("  local_threshold 取 同人P5（尽量覆盖真实同人，且不低于 0.30 下限）；")
    print("  local_margin    取 同人中位 − 异人P95（保区分度）。")

    try:
        Path(a.out).write_text(json.dumps({
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "n_samples": len(records),
            "trusted_groups": [
                {"course": k[0], "speaker_id": k[1],
                 "person_id": pid, "person_name": db.persons[pid]["person_name"],
                 "n": len(groups[k])} for k, pid in trusted.items()],
            "same_dist": {"n": len(same), "median": pct(same, 50), "p5": pct(same, 5),
                          "p25": pct(same, 25), "p75": pct(same, 75),
                          "p95": pct(same, 95),
                          "min": min(same) if same else None,
                          "max": max(same) if same else None},
            "diff_dist": {"n": len(diff), "median": pct(diff, 50), "p5": pct(diff, 5),
                          "p25": pct(diff, 25), "p75": pct(diff, 75),
                          "p95": pct(diff, 95),
                          "min": min(diff) if diff else None,
                          "max": max(diff) if diff else None},
            "separation": sep,
            "same_top1_rate": round(hit_same / max(1, len(same)), 4),
            "recommend": rec,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\n  详细数据已写入：{a.out}")
    except Exception as e:  # noqa: BLE001
        print(f"\n  ⚠️ 报告写入失败：{e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
