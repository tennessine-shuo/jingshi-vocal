# -*- coding: utf-8 -*-
"""会议纪要生成（方案B · 会议分支）。

当飞书课表把一段录音识别为「会议」（课程类型含会议词，或无学生/无授课类型）时，
不再生成教评/专注力报告，而是：
  1. 调用大模型把结构化转写归纳成会议纪要 JSON（议题 / 关键讨论 / 结论决议 / 待办行动项）；
  2. 把纪要 + 思维导图 + 发言统计渲染成**单个自包含 HTML**（内联 CSS/JS，可离线打开/打印）；
  3. 回传 COS。

- 发言统计（每位说话人说了多少次/多少分钟/占比）直接由结构化转写真实统计，
  不由大模型估计（与教评的 engagement 同源）。
- 大模型走与教评相同的 OpenAI 兼容通道（默认腾讯 TokenHub hy3）。
"""
from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from pathlib import Path

from prompts import DEFAULTS, get_prompt, render
from report import _llm_chat

log = logging.getLogger("meeting_summary")

# 提示词已迁到 prompts.py / prompts.json（控制台「提示词」页可编辑，改完下一场会议生效）。
# 这个常量只是向后兼容的别名，唯一来源仍是 prompts.DEFAULTS。
MINUTES_PROMPT = DEFAULTS["meeting_minutes"]["text"]


# ---------------------------------------------------------------- 结构化转写 → 发言统计


def _speaker_stats(segments: list[dict]) -> list[dict]:
    """按说话人统计发言次数 / 总时长 / 占比（真实计算，非大模型估计）。"""
    aggr: "OrderedDict[str, dict]" = OrderedDict()
    total = 0.0
    for s in segments:
        dur = max(0.0, (s.get("end_sec") or 0) - (s.get("start_sec") or 0))
        total += dur
        name = s.get("person_name") or f"说话人{s.get('speaker_id', '?')}"
        a = aggr.setdefault(name, {"name": name, "turns": 0, "seconds": 0.0})
        a["turns"] += 1
        a["seconds"] += dur
    stats = []
    for a in aggr.values():
        stats.append({
            "name": a["name"],
            "turns": a["turns"],
            "minutes": round(a["seconds"] / 60, 1),
            "ratio": round(a["seconds"] / total * 100, 1) if total else 0.0,
        })
    stats.sort(key=lambda x: x["ratio"], reverse=True)
    return stats


# ---------------------------------------------------------------- 大模型纪要


def generate_minutes(segments: list[dict], meta: dict, ctx: dict) -> dict:
    """调用大模型归纳会议纪要；失败时返回降级骨架（附原始摘要提示）。"""
    transcript = "\n".join(
        f"[{s['person_name']} {s['start_sec']}-{s['end_sec']}s] {s['text']}"
        for s in segments
    )
    if not transcript.strip():
        return {"title": "（无有效语音）", "topics": [], "conclusions": [],
                "action_items": [], "summary": "未检测到有效语音，请检查录音设备"}
    try:
        content = _llm_chat([{
            "role": "user",
            "content": render(get_prompt("meeting_minutes", MINUTES_PROMPT),
                              {"transcript": transcript,
                               "meta": json.dumps(meta, ensure_ascii=False)}),
        }], ctx)
        text = content.strip()
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if m:
            text = m.group(1).strip()
        parsed = json.loads(text)
        return {
            "title": parsed.get("title") or "会议纪要",
            "topics": parsed.get("topics") or [],
            "conclusions": parsed.get("conclusions") or [],
            "action_items": parsed.get("action_items") or [],
            "summary": parsed.get("summary") or "",
        }
    except Exception as e:  # noqa: BLE001
        log.warning("会议纪要大模型生成失败，返回骨架: %s", e)
        return {"title": "会议纪要", "topics": [], "conclusions": [],
                "action_items": [], "summary": "（纪要生成失败，仅保留结构化转写）"}


# ---------------------------------------------------------------- 自包含 HTML（含思维导图）


def _esc(txt) -> str:
    return (str(txt or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _mindmap_html(data: dict) -> str:
    """渲染为纯 CSS 横向思维导图（根 → 议题 → 要点），无 JS、离线可用。"""
    title = _esc(data.get("title") or "会议")
    topics = data.get("topics") or []
    if not topics:
        return f"<div class='empty'>（大模型未能提取议题结构，见下方完整纪要）</div>"
    branch = []
    for i, tp in enumerate(topics):
        pts = "".join(
            f"<div class='leaf'>{_esc(p)}</div>" for p in (tp.get("points") or [])
        )
        branch.append(
            f"<div class='col'><div class='b-label'>{_esc(tp.get('name') or ('议题' + str(i + 1)))}</div>{pts}</div>"
        )
    children = "\n".join(branch)
    return f"""<div class="map">
  <div class="root-col"><div class="root">{title}</div></div>
  {children}
</div>"""


def render_meeting_html(data: dict, out_path: Path) -> Path:
    """把纪要数据渲染成单个自包含 HTML（内联 CSS，无外链）。"""
    meta = data.get("meta", {})
    stats = data.get("speaker_stats") or []
    summary = data.get("summary") or ""
    date_cn = (meta.get("start") or "")[:10] or ""
    loc = meta.get("location") or ""
    teacher = meta.get("teacher") or meta.get("teacher_name") or ""

    topic_html = ""
    for i, tp in enumerate(data.get("topics") or []):
        pts = "".join(f"<li>{_esc(p)}</li>" for p in (tp.get("points") or []))
        topic_html += (f"<div class='block'><h3>{i + 1}. {_esc(tp.get('name') or '议题')}</h3>"
                       f"<ul>{pts or '<li class=dim>（无细节）</li>'}</ul></div>")
    conclusions = "".join(f"<li>{_esc(c)}</li>" for c in (data.get("conclusions") or []))
    actions = "".join(
        f"<li><b>{_esc(a.get('who') or '待定')}</b>：{_esc(a.get('task') or '')}"
        f"{('　·　' + _esc(a.get('due'))) if (a.get('due') or '').strip() else ''}</li>"
        for a in (data.get("action_items") or [])
    )
    stats_html = "".join(
        f"<tr><td>{_esc(s['name'])}</td><td>{s['turns']}</td>"
        f"<td>{s['minutes']} 分钟</td><td>{s['ratio']}%</td></tr>"
        for s in stats
    )
    summary_html = (f"<div class='summary'><b>摘要：</b>{_esc(summary)}</div>"
                    if summary else "")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>会议纪要 · {_esc(data.get('title') or '')}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: "Microsoft YaHei", "微软雅黑", sans-serif;
         color: #1f2937; background: #eef2f7; }}
  .page {{ max-width: 980px; margin: 0 auto; padding: 28px 18px 60px; }}
  header {{ background: linear-gradient(135deg,#102a43,#1d4ed8);
           color:#fff; border-radius:14px; padding:22px 26px; }}
  header h1 {{ margin:0 0 8px; font-size:24px; }}
  header .meta {{ opacity:.9; font-size:13px; line-height:1.8; }}
  h2 {{ font-size:19px; color:#102a43; border-left:5px solid #1d4ed8;
       padding-left:10px; margin:26px 0 14px; }}
  .card {{ background:#fff; border-radius:12px; padding:18px 22px;
          box-shadow:0 1px 4px rgba(15,42,67,.08); margin-bottom:16px; }}
  .block {{ margin-bottom:12px; }}
  .block h3 {{ margin:0 0 6px; font-size:15px; color:#1d4ed8; }}
  ul {{ margin:4px 0; padding-left:20px; }}
  li {{ margin:3px 0; line-height:1.7; }}
  .dim {{ color:#9ca3af; }}
  .summary {{ background:#f0f7ff; border-radius:8px; padding:12px 14px;
             font-size:14px; line-height:1.7; color:#1e3a5f; }}
  /* 思维导图 */
  .map {{ display:flex; flex-direction:row; gap:0; align-items:stretch;
         overflow-x:auto; padding:6px 0; }}
  .map .root-col {{ display:flex; align-items:center; }}
  .root {{ background:#102a43; color:#fff; font-weight:700; font-size:16px;
          border-radius:10px; padding:14px 18px; max-width:180px; }}
  .map .col {{ position:relative; border-top:2px solid #93c5fd;
              margin-left:34px; padding:14px 0 4px 18px; min-width:150px; }}
  .map .col:first-of-type {{ border-top-color:transparent; margin-left:26px; }}
  .map .col::before {{ content:""; position:absolute; left:-18px; top:-2px;
              width:16px; height:2px; background:#93c5fd; }}
  .map .col:first-of-type::before {{ display:none; }}
  .b-label {{ font-weight:700; color:#1d4ed8; font-size:14px;
             background:#eff6ff; border-radius:6px; padding:6px 10px;
             display:inline-block; }}
  .map .leaf {{ font-size:12.5px; color:#334155; margin:5px 0 0; padding-left:8px;
               border-left:2px solid #dbeafe; }}
  .empty {{ color:#9ca3af; font-size:13px; }}
  table {{ border-collapse:collapse; width:100%; font-size:13.5px; }}
  th, td {{ border:1px solid #e5e7eb; padding:7px 10px; text-align:left; }}
  th {{ background:#f3f6fb; color:#102a43; }}
  .actions li {{ padding:6px 0; border-bottom:1px dashed #e5e7eb; }}
  footer {{ text-align:center; color:#9ca3af; font-size:12px; margin-top:20px; }}
  @media print {{ body {{ background:#fff; }} .page {{ max-width:100%; }}
                  .card {{ box-shadow:none; border:1px solid #e5e7eb; }} }}
</style></head>
<body><div class="page">
  <header>
    <h1>📋 {_esc(data.get('title') or '会议纪要')}</h1>
    <div class="meta">
      {'时间：' + _esc(date_cn) if date_cn else ''}
      {'　·　地点：' + _esc(loc) if loc else ''}
      {'　·　主持人/讲师：' + _esc(teacher) if teacher else ''}
    </div>
  </header>
  {summary_html}
  <h2>🗺️ 议题结构（思维导图）</h2>
  <div class="card">{_mindmap_html(data)}</div>
  <h2>🗣️ 关键讨论</h2>
  <div class="card">{topic_html or '<div class=empty>（无已归纳议题）</div>'}</div>
  <h2>✅ 结论 / 决议</h2>
  <div class="card"><ul class="actions">{conclusions or '<li class=dim>（无明确结论）</li>'}</ul></div>
  <h2>📌 行动项</h2>
  <div class="card"><ul class="actions">{actions or '<li class=dim>（无待办行动项）</li>'}</ul></div>
  <h2>🎙️ 发言统计</h2>
  <div class="card"><table>
    <tr><th>发言人</th><th>发言次数</th><th>总时长</th><th>占比</th></tr>
    {stats_html or '<tr><td colspan=4 class=dim>（无有效发言）</td></tr>'}
  </table></div>
  <footer>由课堂录制 · 声纹识别 · 大模型纪要自动生成</footer>
</div></body></html>"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    log.info("会议纪要 HTML 已生成: %s", out_path)
    return out_path


def meeting_filename(meta: dict) -> str:
    """会议纪要文件名：会议纪要_YYYYMMDD_课程类型.txt。"""
    date_str = (meta.get("start") or "")[:10].replace("-", "")
    course = meta.get("course_type") or meta.get("course_id") or "会议"
    safe = re.sub(r'[\\/:*?"<>|\s｜│]+', "-", str(course)).strip("-") or "会议"
    return f"会议纪要_{date_str}_{safe}"


# ---------------------------------------------------------------- 顶层封装


def build_meeting(segments: list[dict], meta: dict, ctx: dict) -> dict:
    """生成会议纪要数据（speaker_stats 真实统计 + 大模型 topics/conclusions/actions）。"""
    minutes = generate_minutes(segments, meta, ctx)
    minutes["speaker_stats"] = _speaker_stats(segments)
    minutes["meta"] = meta
    return minutes


def push_meeting(meeting: dict, meta: dict, ctx: dict,
                 files: dict[str, Path] | None = None) -> dict:
    """会议产物回传 COS：{prefix}/{date}/ 下。复用 CosStore。"""
    store = ctx["store"]
    cfg = ctx["config"].get("report", {})
    prefix = cfg.get("result_prefix", "jiaopei/reports")
    date_str = (meta.get("start") or "")[:10].replace("-", "")
    course_id = meta.get("course_id", "unknown")
    keys: dict[str, str] = {}
    json_key = f"{prefix}/{date_str}/{course_id}_meeting.json"
    store.put_json(json_key, {"meta": meta, "meeting": meeting})
    keys["json_key"] = json_key
    for name, path in (files or {}).items():
        p = Path(path)
        if not p.exists():
            continue
        key = f"{prefix}/{date_str}/{p.name}"
        store.put_file(key, str(p))
        keys[name] = key
        log.info("会议产物已回传 COS: %s", key)
    return keys
