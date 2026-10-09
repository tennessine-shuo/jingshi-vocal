# -*- coding: utf-8 -*-
"""离线验证会议分支（不重跑 ASR / 声纹，仅一次 LLM 纪要）：
拉取 COS 上 9/8 例会已生成的转写 → 模拟会议分流 → 生成会议纪要 HTML。
产物输出 C:/recorder/reports/_meeting_test/。
"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cos_client import CosStore
from meeting_summary import build_meeting, render_meeting_html, meeting_filename
from schedule_feishu import match_lesson
from datetime import datetime

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("meeting_test")

cfg = json.loads(Path(r"C:\recorder\cloud\cloud_config.json").read_text(encoding="utf-8"))
store = CosStore(bucket=cfg["cos"]["bucket"], region=cfg["cos"]["region"],
                 secret_id=cfg["cos"]["secret_id"], secret_key=cfg["cos"]["secret_key"])

# 9/8 例会（10:00-11:33 录制，飞书课表 10:00-12:00 例会）
tr = store.get_json("jiaopei/reports/20260908/课1000_transcript.json")
segments = tr["segments"]

meta = {
    "course_id": "课1000", "teacher": "老师", "room_id": "room-01",
    "start": "2026-09-08T10:00:00", "end": "2026-09-08T11:33:03",
    "duration_sec": 5583,
}

# 1) 验证课表匹配判为会议
hit = match_lesson(datetime.fromisoformat(meta["start"]),
                   datetime.fromisoformat(meta["end"]),
                   ctx={"config": cfg},
                   teacher_hint=((cfg.get("feishu") or {}).get("teacher_name") or ""))
log.info("课表匹配: matched=%s is_meeting=%s course=%s",
         hit.get("matched"), hit.get("is_meeting"), hit.get("course_type"))
assert hit.get("matched") and hit.get("is_meeting"), "应判为会议"

# 2) 模拟声纹结果里的 teacher
meta["teacher"] = hit.get("teacher_name") or "老师"

# 3) 会议纪要（真实调用一次 LLM）
ctx = {"store": store, "config": cfg,
       "workdir": r"C:\recorder\cloud_work\_meeting_test"}
Path(ctx["workdir"]).mkdir(parents=True, exist_ok=True)
meeting = build_meeting(segments, meta, ctx)
log.info("会议纪要 title=%s topics=%d conclusions=%d actions=%d",
         meeting.get("title"), len(meeting.get("topics") or []),
         len(meeting.get("conclusions") or []), len(meeting.get("action_items") or []))

# 4) 渲染 HTML
out = Path(r"C:\recorder\reports\_meeting_test")
out.mkdir(parents=True, exist_ok=True)
fjson = out / f"{meeting_filename(meta)}.data.json"
fjson.write_text(json.dumps(meeting, ensure_ascii=False, indent=1), encoding="utf-8")
html = render_meeting_html(meeting, out / f"{meeting_filename(meta)}.html")
log.info("HTML -> %s", html)
log.info("会议分支离线验证完成")
