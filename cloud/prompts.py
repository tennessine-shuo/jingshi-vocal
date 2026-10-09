# -*- coding: utf-8 -*-
"""提示词与报告模板注册表（统一前端可编辑）。

为什么要这个模块：
- 原来提示词是硬编码在 report.py / meeting_summary.py 里的字符串常量，
  想改一句话就得改源码、还要重启流水线；控制台也没地方看当前用的是什么词。
- 现在拆成『默认值在代码里 + 覆盖值在 prompts.json』两层：
  * 默认值（DEFAULTS）永远可用 —— prompts.json 丢了/写坏了也不会让流水线停摆；
  * 控制台只写 prompts.json，不碰源码，改完**下一门课立即生效**（无需重启）。

热生效原理：get_prompt() 每次调用都 stat 一次文件 mtime，变了才重新读盘。
所以流水线常驻进程里改完提示词，下一个课程就用新词，不必重启。

占位符写法（两种都支持，见 render()）：
  * {transcript}          —— 已知变量直接替换，未知的 {xxx} 原样保留（不报错、不吞掉）
  * {{ "key": "value" }}  —— 兼容 str.format 时代的双花括号转义，会还原成单花括号
  想直接写 {"key": "value"}（单花括号）也可以，不必转义。
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

log = logging.getLogger("prompts")

CLOUD_DIR = Path(__file__).resolve().parent
PROMPTS_FILE = CLOUD_DIR / "prompts.json"
BACKUP_DIR = CLOUD_DIR / "prompts_backup"


# ---------------------------------------------------------------- 默认模板
# ⚠️ 这些默认值必须与上线时线上跑的词逐字一致，否则"恢复默认"会改变报告内容。

_TEACHING_EVAL = """你是资深教研专家，请依据课堂转写做教学内容评估。
只输出严格 JSON，不要输出其他任何内容（不要 markdown 代码块标记），结构如下：
{{
  "content_text": "上课内容概括（约400字，按教学流程分阶段描述：先…随后…接着…最后…；纯文字）",
  "knowledge_text": "知识小结（详细：本课核心概念、公式与解题方法、常见易错点；纯文字，分条用「- 」前缀）",
  "summary": "上课内容简要概括（200字以内，用于图表标注，不是 content_text 的复制）",
  "learning_progress": {{"overall": 0-100的整数, "topics": [{{"name": "知识点", "progress": 0-100}}]}},
  "error_analysis": [{{"type": "错误类型（如 概念理解/计算失误/审题偏差/方法选择/表达不规范）", "count": 次数整数, "rate": 占全部错误百分比整数}}],
  "knowledge_mastery": [{{"name": "知识点", "level": 1-5的整数}}],
  "time_allocation": [{{"phase": "教学环节（如 知识回顾/新知讲授/例题精讲/课堂练习/总结答疑）", "minutes": 分钟数整数}}]
}}
规则：只依据转写证据评估，不臆测；time_allocation 各环节分钟数之和约等于实际课长；
error_analysis 的 rate 之和为 100（无错误则给空数组 []）；
topics / knowledge_mastery 取本课真实讲授的 3-6 个知识点；
若转写内容过少或无明显教学行为，所有数组给空，content_text 说明原因。

课程信息：{meta}
课堂特征：{stats}
课堂转写（说话人 [起-止秒] 文本）：
{transcript}"""

_CHUNK_SUMMARY = """你是教研专家，请对以下课堂片段输出要点摘要（讲授了哪些知识点、学生的错误与疑问、各环节大致时长）。
{chunk}"""

_MEETING_MINUTES = """你是会议秘书，请根据会议转写生成结构化的会议纪要。
只输出严格 JSON，不要任何其他内容（不要 markdown 代码块标记），结构如下：
{{
  "title": "会议标题（一句话概括，去掉冗余，如『校区例会』）",
  "topics": [
    {{"name": "议题/主题（如 学生档案页面进度）", "points": ["讨论要点1", "讨论要点2"]}}
  ],
  "conclusions": ["明确的结论或决定1", "决定2"],
  "action_items": [
    {{"who": "负责人姓名（转写中有姓名才写，否则留空）", "task": "要做的具体事项", "due": "截止/时限（无则空）"}}
  ],
  "summary": "整场会议不超过200字的摘要（用于导图根节点旁标注）"
}}
规则：只依据转写证据归纳，不臆测；topics 取 3~6 个真实讨论到的议题；
没有明确结论或行动项时给空数组；summary 用纯文字。
title 用会议真实内容起名（如『校区例会』），**不要**包含课程序号/内部编号（如 课1000、10:00-12:00 之类）。
会议信息：{meta}
转写（说话人 [起-止秒] 文本）：
{transcript}"""

_TXT_SKELETON = """教评报告  {date_cn}  {student}  {course}

上课内容

{content_text}

知识小结

{knowledge_text}"""


# key -> {name, desc, vars(可用占位符), required(vars 中缺了会真实出问题的), values, text}
DEFAULTS: dict[str, dict] = {
    "teaching_eval": {
        "name": "教评报告 · 主提示词",
        "desc": "决定文字版与 PDF 报告的**内容**：上课内容、知识小结、知识点进度、"
                "错误分析、掌握度、时间分配。改这里等于改报告写了什么。",
        "vars": ["meta", "stats", "transcript"],
        "required": ["transcript"],
        "values": {
            "meta": '{"student_name": "学生乙", "course_type": "IG物理", '
                    '"grade": "IG2", "start": "2026-09-15T16:30:00"}',
            "stats": '{"duration_min": 120, "n_turns": 96, "teacher_ratio": 0.62}',
            "transcript": "[教师 0-14s] 今天讲牛顿第二定律，先看这个例题。\n"
                          "[学生乙 15-22s] 老师，这里的合力怎么算？\n"
                          "[教师 23-58s] 好问题，我们先把受力分析画出来……",
        },
        "text": _TEACHING_EVAL,
    },
    "chunk_summary": {
        "name": "长课分片摘要",
        "desc": "转写超过 8000 字时先分片摘要、再合并成总报告（map-reduce）。"
                "改这里只影响长课的中间摘要质量，不影响最终结构。",
        "vars": ["chunk"],
        "required": ["chunk"],
        "values": {
            "chunk": "[教师 0-14s] 今天讲牛顿第二定律……\n[学生乙 15-22s] 合力怎么算？",
        },
        "text": _CHUNK_SUMMARY,
    },
    "meeting_minutes": {
        "name": "会议纪要",
        "desc": "课表判定为「会议」时走这条分支（不出教评，出会议纪要 HTML）。"
                "决定议题、结论、待办行动项怎么提炼。",
        "vars": ["meta", "transcript"],
        "required": ["transcript"],
        "values": {
            "meta": '{"course_id": "校区例会", "start": "2026-09-15T10:00:00"}',
            "transcript": "[教师 0-30s] 先过一下学生档案页面的进度。\n"
                          "[学生甲 31-55s] 这周能出第一版。",
        },
        "text": _MEETING_MINUTES,
    },
    "eval_txt": {
        "name": "文字版报告 · 骨架",
        "desc": "决定导出 txt 的**格式**（发给家长/微信的那份纯文字报告）。"
                "不是给大模型看的，是套在结果外面的排版壳。",
        "vars": ["date_cn", "student", "course", "content_text", "knowledge_text"],
        "required": ["content_text"],
        "values": {
            "date_cn": "2026年9月15日",
            "student": "学生乙",
            "course": "IG物理",
            "content_text": "（由主提示词生成的上课内容，约400字）",
            "knowledge_text": "- 牛顿第二定律：F=ma\n- 受力分析步骤：先重力、再弹力、后摩擦力",
        },
        "text": _TXT_SKELETON,
    },
}

# 大模型必须原样产出的 JSON 键 —— 缺了会让报告对应板块空白（只警告，不阻止保存）
EXPECTED_KEYS = {
    "teaching_eval": ["content_text", "knowledge_text", "summary",
                      "learning_progress", "error_analysis",
                      "knowledge_mastery", "time_allocation"],
    "meeting_minutes": ["title", "topics", "conclusions", "action_items", "summary"],
}

_VAR_RE = re.compile(r"\{(\w+)\}")


# ---------------------------------------------------------------- 渲染


def render(template: str, values: dict) -> str:
    """渲染模板。未知占位符原样保留，不抛异常。

    只替换『已知变量名』，所以 JSON 里的 {"name": ...} 不会被误伤。
    ⚠️ 花括号还原只在模板**出现 {{ 时**才做 —— 因为 {{ }} 是 str.format 时代的转义写法，
    不还原就会原样发给大模型；而如果模板里根本没有 {{（用户直接写单花括号 JSON），
    就绝不能做还原：JSON 里 {"a": {"b": 1}} 结尾的 }} 会被误吃一个（实测踩过）。
    """
    text = str(template)
    if "{{" in text:
        text = text.replace("{{", "{").replace("}}", "}")
    return _VAR_RE.sub(
        lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
        text)


def missing_vars(key: str, template: str, required: bool | None = None) -> list[str]:
    """模板里没写、但代码会传值的占位符。

    required=True  只看必需项（缺了会让报告缺核心数据）
    required=False 只看可选项（缺了只是少点信息，允许）
    required=None  全部
    """
    meta = DEFAULTS.get(key) or {}
    allv = list(meta.get("vars", []))
    req = list(meta["required"]) if "required" in meta else list(allv)
    opt = [v for v in allv if v not in req]
    pool = req if required is True else opt if required is False else allv
    return [v for v in pool if f"{{{v}}}" not in str(template)]


def preview(key: str, template: str, limit: int = 0) -> str:
    """用示例数据渲染，给控制台做零成本预览。"""
    values = (DEFAULTS.get(key) or {}).get("values", {})
    text = render(template, values)
    return text[:limit] if limit else text


def validate(key: str, template: str) -> tuple[bool, str]:
    """保存前校验。返回 (是否可保存, 提示语)。

    分级处理：
      * 缺**必需**占位符 → 拒绝保存（会让报告缺核心数据，属于真错误）
      * 缺**可选**占位符 → 允许保存，只提醒（用户可能就是要精简）
      * JSON 字段名缺失 / 花括号不配平 → 允许保存，只提醒
    """
    if not str(template).strip():
        return False, "内容为空"
    miss_req = missing_vars(key, template, True)
    if miss_req:
        return False, ("缺少必需占位符：" + "、".join("{%s}" % v for v in miss_req)
                       + "\n\n这是报告的核心数据来源，删掉后该部分会缺失或明显变差。")
    # 渲染一遍，确认不会出意外（render 本身不抛异常，这里防的是类型问题）
    try:
        out = preview(key, template)
    except Exception as exc:  # noqa: BLE001
        return False, f"渲染失败：{exc}"
    if not out.strip():
        return False, "渲染结果为空，请检查模板内容"
    warn = []
    miss_opt = missing_vars(key, template, False)
    if miss_opt:
        warn.append("⚠ 未使用这些可用数据：" + "、".join("{%s}" % v for v in miss_opt)
                    + "（不影响运行，但报告会少这些信息）")
    if key in EXPECTED_KEYS:
        lost = [k for k in EXPECTED_KEYS[key] if f'"{k}"' not in out]
        if lost:
            warn.append("⚠ 以下 JSON 字段名未出现，报告对应板块可能变空："
                        + "、".join(lost))
    # 花括号配平检查（未配对会让大模型收到残缺的 JSON 示例）
    if out.count("{") != out.count("}"):
        warn.append("⚠ 花括号不配平（{ 与 } 数量不等），请确认 JSON 示例写完整")
    return True, "\n".join(warn) if warn else ""


# ---------------------------------------------------------------- 读写


_cache: dict = {"mtime": None, "data": None}


def _read_overrides() -> dict:
    try:
        if PROMPTS_FILE.exists():
            raw = json.loads(PROMPTS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw.get("prompts") or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("prompts.json 读取失败，使用默认提示词：%s", exc)
    return {}


def load(force: bool = False) -> dict:
    """返回完整模板表（默认值 + 覆盖值），带 mtime 缓存。"""
    try:
        mtime = PROMPTS_FILE.stat().st_mtime if PROMPTS_FILE.exists() else 0.0
    except OSError:
        mtime = 0.0
    if force or _cache["mtime"] != mtime or _cache["data"] is None:
        overrides = _read_overrides()
        data = {}
        for key, meta in DEFAULTS.items():
            ov = overrides.get(key)
            text = ov.get("text") if isinstance(ov, dict) else (
                ov if isinstance(ov, str) else None)
            data[key] = {**meta, "text": text if isinstance(text, str) and text.strip()
                         else meta["text"]}
            data[key]["modified"] = bool(isinstance(text, str) and text.strip()
                                         and text != meta["text"])
        _cache.update(mtime=mtime, data=data)
    return _cache["data"]


def get_prompt(key: str, default: str = "") -> str:
    """取单个模板文本（热生效入口，report.py / meeting_summary.py 用这个）。"""
    item = load().get(key)
    return item["text"] if item else default


def save(key: str, text: str) -> tuple[bool, str]:
    """保存单个模板（先校验，后原子写 + 备份上一版）。"""
    if key not in DEFAULTS:
        return False, f"未知模板：{key}"
    ok, msg = validate(key, text)
    if not ok:
        return False, msg
    overrides = dict(_read_overrides())
    overrides[key] = {"text": text, "updated_at": datetime.now().isoformat(timespec="seconds")}
    try:
        PROMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        if PROMPTS_FILE.exists():
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(PROMPTS_FILE, BACKUP_DIR / f"prompts_{stamp}.json")
        tmp = PROMPTS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"prompts": overrides}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(PROMPTS_FILE)
    except Exception as exc:  # noqa: BLE001
        return False, f"写入失败：{exc}"
    load(force=True)
    return True, msg or "已保存"


def reset(key: str) -> tuple[bool, str]:
    """恢复单个模板为默认值。"""
    if key not in DEFAULTS:
        return False, f"未知模板：{key}"
    overrides = dict(_read_overrides())
    if key not in overrides:
        return True, "本来就是默认值"
    overrides.pop(key, None)
    try:
        tmp = PROMPTS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"prompts": overrides}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(PROMPTS_FILE)
    except Exception as exc:  # noqa: BLE001
        return False, f"写入失败：{exc}"
    load(force=True)
    return True, "已恢复默认"


def status() -> dict:
    """给控制台的状态摘要。"""
    data = load()
    modified = [k for k, v in data.items() if v.get("modified")]
    return {
        "file": str(PROMPTS_FILE),
        "exists": PROMPTS_FILE.exists(),
        "updated_at": (datetime.fromtimestamp(PROMPTS_FILE.stat().st_mtime)
                       .isoformat(timespec="seconds")
                       if PROMPTS_FILE.exists() else ""),
        "items": [{"key": k, "name": v["name"], "modified": v.get("modified", False),
                   "chars": len(v["text"])} for k, v in data.items()],
        "modified_count": len(modified),
    }
