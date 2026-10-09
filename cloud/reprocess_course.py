# -*- coding: utf-8 -*-
"""复跑已有转写的课程（跳过 ASR，不产生识别费用）：
下载 COS 上已有的 transcript/meta → 飞书课表匹配 → 专注力 → 教评(LLM)
→ 本地三件套 + 教评 txt/pdf → 回传 COS（覆盖旧格式报告）。

用法: python reprocess_course.py <date_yyyymmdd> <course_id>
"""
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cos_client import CosStore
from focus import (compute_focus, render_focus_html, update_focus_entry_xlsx,
                   write_focus_report_xlsx)
from pipeline import Pipeline
from report import (build_pdf_data, eval_txt_filename, generate_report,
                    push_report, render_pdf, write_eval_txt)
from stages import compute_stats

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("reprocess")


def main(date_str: str, course_id: str) -> None:
    cfg = json.loads(Path(r"C:\recorder\cloud\cloud_config.json").read_text(encoding="utf-8"))
    store = CosStore(bucket=cfg["cos"]["bucket"], region=cfg["cos"]["region"],
                     secret_id=cfg["cos"]["secret_id"], secret_key=cfg["cos"]["secret_key"])
    prefix = cfg.get("report", {}).get("result_prefix", "jiaopei/reports")

    tr_key = f"{prefix}/{date_str}/{course_id}_transcript.json"
    log.info("下载转写 %s", tr_key)
    tr = store.get_json(tr_key)
    segments = tr["segments"]
    speakers = tr.get("speakers")

    # meta：从 room 目录找该课程的 meta 键
    room_prefix = f"{cfg['cos'].get('prefix', 'jiaopei')}/room-01/{date_str}/"
    meta_key = next(k for k in store.list_keys(room_prefix)
                    if k.startswith(f"{room_prefix}{course_id}_") and k.endswith("_meta.json"))
    meta = store.get_json(meta_key)
    log.info("meta=%s 段数=%d", meta_key, len(segments))

    pipe = Pipeline(cfg)
    ctx = {"store": store, "config": cfg,
           "workdir": str(Path(r"C:\recorder\cloud_work") / course_id)}
    Path(ctx["workdir"]).mkdir(parents=True, exist_ok=True)

    # 1) 课表匹配（传入声纹 speaker，识别教师 → 判上课/会议/仅存档）
    meta = pipe._enrich_meta(dict(meta), speakers or [])
    log.info("课表匹配: matched=%s is_meeting=%s student=%s course=%s reason=%s",
             meta.get("schedule_matched"), meta.get("is_meeting"),
             meta.get("student_name"), meta.get("course_type"),
             meta.get("schedule_reason"))

    # 复用 pipeline 的任务对象与三分支产出逻辑（与主流程完全一致）
    task = pipe._make_task(pipe.store, "", meta)
    task.segments = segments
    task.stats = compute_stats(segments, meta, ctx)
    task.meta = meta
    date_str = (meta.get("start") or "")[:10].replace("-", "")

    if not task.meta.get("schedule_matched"):
        # 声纹/课表无法确定 → 仅存档转写与统计
        log.info("复跑：未确定上课/会议（%s），仅存档", task.meta.get("schedule_reason"))
        result = pipe._archive_transcript(task, speakers or [], date_str)
    elif task.meta.get("is_meeting"):
        log.info("复跑：会议 → 生成会议纪要 + 思维导图")
        result = pipe._produce_meeting(task, speakers or [], date_str, ctx)
    else:
        log.info("复跑：上课 → 生成教评 + 专注力")
        result = pipe._produce_lesson(task, speakers or [], date_str, ctx)

    # 更新 transcript.json（补 stats/focus）
    store.put_json(tr_key, {"course_id": course_id, "segments": segments,
                            "stats": task.stats, "focus": task.focus,
                            "speakers": speakers})
    log.info("COS 产物: %s", result)

    # 清理旧格式报告键（best-effort）
    for stale in (f"{prefix}/{date_str}/{course_id}_report.txt",):
        try:
            store.delete(stale)
            log.info("已删除旧键 %s", stale)
        except Exception as e:  # noqa: BLE001
            log.warning("删除旧键失败 %s: %s", stale, e)
    log.info("复跑完成 course=%s status=%s", course_id, task.status)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("用法: python reprocess_course.py <date_yyyymmdd> <course_id>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
