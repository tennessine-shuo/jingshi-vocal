# -*- coding: utf-8 -*-
"""补跑失败课程：学生乙-IG物理 / 学生丁-IG物理（2026-09-11）。

背景：2026-09-11 两次任务都因混元 hy3 免费额度耗尽（HTTP 402）在最后一步失败，
     前五步（录音/上传/ASR/声纹/课表）全部成功，产物被归档为 archived。
现已把 llm.tokenhub_model 切换为 deepseek-v4-flash，本脚本据此补出教评报告。

两条路径共用同一个 _run()，但"句级列表从哪来"不同：

  学生丁 —— 本地已有完整转录（669 段）→ 跳过 ASR，零识别费用把
           [{person_id,...,text}] 反推回 [{start_sec,end_sec,text,speaker_id}]，
           再走 clean_segments 重建，保证与主流程产物结构完全一致。

  学生乙 —— 转录未落盘，但 COS 上 `<course_id>_merged_16k.wav` 仍在
           → 下载合并件 → 设 ctx["merged_wav"] / ctx["merged_url"]
           → 重跑 ASR（16k_zh_en_2.0，约 0.8 元/h × 1.8h ≈ 1.4 元）。

用法:
    python repair_20260911.py gavin     # 只补 学生丁（零成本）
    python repair_20260911.py lucas     # 只补 学生乙（需 ASR）
    python repair_20260911.py both
"""
from __future__ import annotations

import copy
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pipeline import Pipeline
from stages import (asr_transcribe, clean_segments, compute_stats, diarize,
                    voiceprint_match)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("repair")

CFG_PATH = Path(r"C:\recorder\cloud\cloud_config.json")
REPORTS = Path(r"C:\recorder\reports\20260911")
DATE = "20260911"
ROOM = "room-01"

CASES = {
    "gavin": {"course_id": "学生丁-IG物理",
              "local": REPORTS / "archived_20260911_学生丁-IG物理_transcript.json"},
    "lucas": {"course_id": "学生乙-IG物理", "local": None},
}


def _load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def _to_raw_segments(cleaned: list[dict]) -> list[dict]:
    """把 clean_segments 产物反推回句级输入（保留原 speaker_id / 文本）。

    注意：clean_segments 合并同人相邻句时丢了逐句边界，这里把合并后的段
    当"一句话"回填，喂回 clean_segments 会因其内部 3s 合并规则再次合并，
    结果与原始产物一致（幂等），不会二次膨胀。
    """
    return [{"speaker_id": s.get("speaker_id", 0),
             "start_sec": s["start_sec"], "end_sec": s["end_sec"],
             "text": s["text"]} for s in cleaned]


def _speakers_from_segments(cleaned: list[dict]) -> list[dict]:
    """重建 speakers 列表（clean_segments 已把身份写进每段，去重即可）。"""
    seen: dict[int, dict] = {}
    for s in cleaned:
        sid = s.get("speaker_id", 0)
        if sid in seen:
            continue
        seen[sid] = {"speaker_id": sid,
                     "start_sec": s["start_sec"], "end_sec": s["end_sec"],
                     "person_id": s.get("person_id", "unregistered"),
                     "person_name": s.get("person_name", "未识别学生"),
                     "role": s.get("role", "student"),
                     "confidence": 0.6 if s.get("role") == "teacher" else 0.0,
                     "method": "reused"}
    return list(seen.values())


def _prepare_from_merged(pipe: Pipeline, ctx: dict, meta: dict) -> tuple[list, list]:
    """学生乙 路径：优先用本地 ASR 缓存 → 否则下载合并件重跑 ASR → 声纹 → 清洗。

    本地缓存 cloud_work/<course_id>/asr_cache.json 保存句级结果，避免反复
    重跑 ASR 重复计费（一次约 1.4 元）。
    """
    store = ctx["store"]
    course_id = meta["course_id"]
    cache = Path(ctx["workdir"]) / "asr_cache.json"
    merged_key = f"jiaopei/_pipeline/{ROOM}/{DATE}/{course_id}_merged_16k.wav"
    local = Path(ctx["workdir"]) / "merged_16k.wav"
    if not local.exists():
        log.info("下载合并件: %s", merged_key)
        store.download(merged_key, str(local))
    log.info("合并件 %.1f MB", local.stat().st_size / 1024 / 1024)

    # _run_asr_task 依赖这两个 ctx 键；不设则 KeyError
    ctx["merged_wav"] = str(local)
    ctx["merged_url"] = store.presigned_url(merged_key, expired=6 * 3600)

    if cache.exists():
        ctx["_asr"] = json.loads(cache.read_text(encoding="utf-8"))
        log.info("复用 ASR 缓存: %d 句（跳过识别计费）", len(ctx["_asr"]))
    else:
        diarize(meta, ctx)                     # 触发 ASR，结果写入 ctx["_asr"]
        cache.write_text(json.dumps(ctx["_asr"], ensure_ascii=False),
                         encoding="utf-8")
        log.info("ASR 已缓存: %d 句", len(ctx["_asr"]))

    timeline = diarize(meta, ctx)              # 复用 ctx["_asr"]
    speakers = voiceprint_match(timeline, meta, ctx)
    raw = asr_transcribe(timeline, meta, ctx)  # vad_segments 形参实际未用，返回句级列表
    segments = clean_segments(raw, speakers)
    distinct = {s["speaker_id"]: (s["role"], s["person_name"]) for s in speakers}
    log.info("ASR 重建: %d 句 → %d 段，说话人 %s",
             len(raw), len(segments), distinct)
    return segments, speakers


def _prepare_from_local(transcript: dict) -> tuple[list, list]:
    """学生丁 路径：复用本地转录，零成本，不碰 ASR。"""
    cleaned = transcript["segments"]
    speakers = _speakers_from_segments(cleaned)
    segments = clean_segments(_to_raw_segments(cleaned), speakers)
    log.info("复用本地转录: %d 段 → %d 段，说话人 %s",
             len(cleaned), len(segments),
             [(s["speaker_id"], s["role"], s["person_name"]) for s in speakers])
    return segments, speakers


def _run(case_key: str) -> dict:
    case = CASES[case_key]
    course_id = case["course_id"]
    cfg = _load_cfg()
    pipe = Pipeline(cfg)
    ctx = pipe._new_ctx(course_id)
    store = ctx["store"]

    # ---- 1) 取基础 meta ----
    transcript = None
    if case["local"] and case["local"].exists():
        transcript = json.loads(case["local"].read_text(encoding="utf-8"))
        meta = copy.deepcopy(transcript.get("meta") or {})
    else:
        room_prefix = f"jiaopei/{ROOM}/{DATE}/"
        meta_key = next(k for k in store.list_keys(room_prefix)
                        if k.startswith(f"{room_prefix}{course_id}_")
                        and k.endswith("_meta.json"))
        meta = store.get_json(meta_key)
    log.info("基础 meta: course=%s start=%s end=%s", course_id,
             meta.get("start"), meta.get("end"))

    # ---- 2) 拿到 segments / speakers ----
    if transcript is not None:
        segments, speakers = _prepare_from_local(transcript)
    else:
        segments, speakers = _prepare_from_merged(pipe, ctx, meta)

    # ---- 3) 课表匹配（声纹教师驱动）----
    # 注意：_enrich_meta 会重算 teacher（从 speakers 的 role=teacher 取名）
    # 飞书 lark-cli 有频控，连续调用会返回 800030005 not_found（实为限流），
    # 这里退避重试，避免把限流误判成"未匹配 → 仅存档"。
    for attempt in range(4):
        meta = pipe._enrich_meta(dict(meta), speakers)
        if meta.get("schedule_matched"):
            break
        if meta.get("schedule_reason") != "query_failed":
            break
        wait = 20 * (attempt + 1)
        log.warning("课表查询失败（疑似飞书限流），%ds 后重试 %d/3", wait, attempt + 1)
        time.sleep(wait)
    log.info("课表匹配: matched=%s is_meeting=%s student=%s course=%s reason=%s",
             meta.get("schedule_matched"), meta.get("is_meeting"),
             meta.get("student_name"), meta.get("course_type"),
             meta.get("schedule_reason"))
    if not meta.get("schedule_matched"):
        log.warning("未匹配到课表（%s）—— 不生成报告，保持归档", meta.get("schedule_reason"))

    # ---- 4) 走与主流程完全相同的分支 ----
    task = pipe._make_task(store, "", meta)
    task.segments = segments
    task.stats = compute_stats(segments, meta, ctx)
    task.meta = meta
    date_str = (meta.get("start") or "")[:10].replace("-", "") or DATE

    if not meta.get("schedule_matched"):
        result = pipe._archive_transcript(task, speakers, date_str)
    elif meta.get("is_meeting"):
        log.info("→ 会议分支")
        result = pipe._produce_meeting(task, speakers, date_str, ctx)
    else:
        log.info("→ 上课分支（教评 + 专注力）")
        result = pipe._produce_lesson(task, speakers, date_str, ctx)

    # ---- 5) 回写 transcript（保留 archived 原件不动）----
    prefix = cfg.get("report", {}).get("result_prefix", "jiaopei/reports")
    tr_key = f"{prefix}/{date_str}/{course_id}_transcript.json"
    store.put_json(tr_key, {"course_id": course_id, "meta": meta,
                            "segments": segments, "stats": task.stats,
                            "focus": task.focus, "speakers": speakers})
    log.info("完成 course=%s status=%s", course_id, task.status)
    log.info("本地产物: %s", {k: str(v) for k, v in task.files.items()})
    return {"status": task.status, "result": result,
            "files": {k: str(v) for k, v in task.files.items()}}


if __name__ == "__main__":
    what = (sys.argv[1] if len(sys.argv) > 1 else "both").lower()
    t0 = time.time()
    for key in ("gavin", "lucas"):
        if what in (key, "both"):
            log.info("=" * 64)
            log.info("开始补跑 %s", key.upper())
            _run(key)
    log.info("全部完成，耗时 %.0f 秒", time.time() - t0)
