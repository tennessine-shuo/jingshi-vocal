# -*- coding: utf-8 -*-
"""离线验证新链路（不重复跑 ASR，不产生识别费用）：
拉取 COS 上已有的 TEST-001 转写 → 课表匹配 → 专注力计算 → 教评(LLM) →
本地三件套 + 教评 txt/pdf。产物输出到 C:/recorder/reports/_offline_test/。
"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cos_client import CosStore
from focus import (compute_focus, render_focus_html, update_focus_entry_xlsx,
                   write_focus_report_xlsx)
from pipeline import Pipeline
from report import (build_pdf_data, eval_txt_filename, generate_report,
                    render_pdf, write_eval_txt)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("offline_test")

cfg = json.loads(Path(r"C:\recorder\cloud\cloud_config.json").read_text(encoding="utf-8"))
store = CosStore(bucket=cfg["cos"]["bucket"], region=cfg["cos"]["region"],
                 secret_id=cfg["cos"]["secret_id"], secret_key=cfg["cos"]["secret_key"])

TR_KEY = "jiaopei/reports/20260904/TEST-001_transcript.json"
META_KEY = "jiaopei/room-01/20260904/TEST-001_20260904_203030_meta.json"

log.info("下载转写与 meta ...")
tr = store.get_json(TR_KEY)
segments = tr["segments"]
try:
    meta = store.get_json(META_KEY)
except Exception:
    meta = {"course_id": "TEST-001", "teacher": "老师",
            "start": "2026-09-04T20:30:30", "end": "2026-09-04T21:30:30",
            "duration_sec": 3600, "room_id": "room-01"}
log.info("转写 %d 段", len(segments))

pipe = Pipeline(cfg)
ctx = {"store": store, "config": cfg, "workdir": r"C:\recorder\cloud_work\_offline_test"}
Path(ctx["workdir"]).mkdir(parents=True, exist_ok=True)

# 1) 课表匹配
meta = pipe._enrich_meta(dict(meta))
log.info("课表匹配: matched=%s student=%s course=%s grade=%s loc=%s",
         meta.get("schedule_matched"), meta.get("student_name"),
         meta.get("course_type"), meta.get("grade"), meta.get("location"))

# 2) 专注力
focus = compute_focus(segments, meta, {"duration_sec": meta.get("duration_sec", 0)})
log.info("专注力: separated=%s teach=%s active=%s focus=%s match=%s",
         focus["separated"], focus["teach_score"], focus["active_score"],
         focus["focus_score"], focus["match_score"])
log.info("insight: %s", focus["insight"])

# 3) 教评（LLM，真实调用一次）
from stages import compute_stats
stats = compute_stats(segments, meta, {"duration_sec": meta.get("duration_sec", 0)})
report = generate_report(segments, stats, meta, ctx, focus)
log.info("教评 keys: %s", list(report.keys()))

# 4) 本地产出
out = Path(r"C:\recorder\reports\_offline_test")
out.mkdir(parents=True, exist_ok=True)
txt_path = out / eval_txt_filename(meta)
txt_path.write_text(write_eval_txt(report, meta), encoding="utf-8")
log.info("txt -> %s", txt_path)

pdf_data = build_pdf_data(report, meta, focus)
pdf = render_pdf(pdf_data, out / txt_path.name.replace(".txt", ".pdf"))
log.info("pdf -> %s", pdf)

fjson = out / "focus_data.json"
fjson.write_text(json.dumps(focus, ensure_ascii=False, indent=1), encoding="utf-8")
h = render_focus_html(focus, out / "专注力报告_TEST-001.html")
x = write_focus_report_xlsx(focus, out / "专注力报告_TEST-001.xlsx")
e = update_focus_entry_xlsx(focus, out / "专注力数据录入表.xlsx")
log.info("focus files -> %s %s %s", h.name, x.name, e.name)
log.info("离线验证完成")
