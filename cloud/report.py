# -*- coding: utf-8 -*-
"""教评报告生成：大模型调用 + 文字版/PDF 产出 + 推送（方案B · 报告层 · 正式版 v2）。

v2 变更：报告内容与 teaching-eval 自动化工作流对齐——
- 文字版：「上课内容（约400字，分阶段）+ 知识小结（详细）」两段式纯文本，
  无任何 Markdown 符号，文件名 教评报告_YYYYMMDD_学生名_课程类型.txt，可直接发微信
- 可视化 PDF：复用 teaching-eval 技能的 generate_pdf.py（严禁修改，仅调用），
  数据结构与其一致（learning_progress / engagement / error_analysis /
  knowledge_mastery / time_allocation / summary）
- engagement（发言/提问/专注度/参与率）直接用专注力引擎的真实计算值，不由大模型估计

- 输入：结构化转写（谁·何时·说了什么）+ 课堂特征统计 + 课程元数据（已被飞书课表 enrichment）
- 长课（转写超 8000 字）采用 map-reduce：先分片摘要，再合并成总报告
- 大模型默认走腾讯 TokenHub（OpenAI 兼容协议），也可配置其他 openai_compatible 接口
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import requests

from prompts import DEFAULTS, get_prompt, render

log = logging.getLogger("report")

# 提示词已迁到 prompts.py / prompts.json，可在控制台「提示词」页编辑，
# 改完**下一门课立即生效**（get_prompt 每次带 mtime 缓存读盘，不必重启流水线）。
# 这两个常量只是向后兼容的别名（旧脚本 import 它们不会炸），唯一来源仍是 prompts.DEFAULTS。
PROMPT_TEMPLATE = DEFAULTS["teaching_eval"]["text"]
CHUNK_PROMPT = DEFAULTS["chunk_summary"]["text"]

# time_allocation 蓝色系（深→浅，循环使用，teaching-eval 规范）
_PHASE_COLORS = [[16, 42, 86], [37, 99, 235], [59, 130, 246], [96, 165, 250], [147, 197, 253]]

# teaching-eval 技能的 PDF 生成器（只调用，不修改）。
# 安装包把该脚本捆绑到 <root>\vendor\teaching_eval\generate_pdf.py，
# 有捆绑副本时优先用；否则回落到开发机的技能目录。环境变量始终最高优先级。
_BUNDLED_PDF_GEN = (Path(__file__).resolve().parent.parent
                    / "vendor" / "teaching_eval" / "generate_pdf.py")
_PDF_GENERATOR = os.getenv(
    "TEACHING_EVAL_PDF_GEN",
    str(_BUNDLED_PDF_GEN) if _BUNDLED_PDF_GEN.exists()
    else r"C:\Users\汤朔\.workbuddy\skills\teaching-eval\assets\generate_pdf.py")


# ---------------------------------------------------------------- LLM 通道


def _llm_chat(messages: list[dict], ctx: dict) -> str:
    llm_cfg = ctx["config"].get("llm", {})
    provider = llm_cfg.get("provider", "tokenhub")
    if provider in ("tokenhub", "hunyuan"):
        return _openai_compatible_chat(messages, llm_cfg, tokenhub=True)
    return _openai_compatible_chat(messages, llm_cfg)


def _openai_compatible_chat(messages: list[dict], llm_cfg: dict,
                            tokenhub: bool = False) -> str:
    """OpenAI 兼容接口。tokenhub=True 时走腾讯云 TokenHub（hy3-preview 等）。"""
    if tokenhub:
        url = llm_cfg.get("tokenhub_base_url",
                          "https://tokenhub.tencentmaas.com/v1/chat/completions")
        key = llm_cfg.get("tokenhub_api_key") or os.getenv("TOKENHUB_API_KEY", "")
        model = llm_cfg.get("tokenhub_model", "hy3-preview")
        if not key:
            raise RuntimeError(
                "缺少 TokenHub API Key：请在 TokenHub 控制台创建 sk- Key 后，"
                "填入 cloud_config.json 的 llm.tokenhub_api_key")
    else:
        url = llm_cfg.get("openai_base_url",
                          os.getenv("LLM_BASE_URL",
                                    "https://api.deepseek.com/v1/chat/completions"))
        key = llm_cfg.get("openai_api_key") or os.getenv("LLM_API_KEY", "")
        model = llm_cfg.get("openai_model",
                            os.getenv("LLM_MODEL", "deepseek-chat"))
        if not key:
            raise RuntimeError("缺少大模型密钥（openai_api_key 或 LLM_API_KEY）")
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": messages, "temperature": 0.3},
        timeout=180,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"LLM 请求失败 HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"]


def _chunk(text: str, max_chars: int = 8000) -> list[str]:
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) > max_chars:
            chunks.append(cur)
            cur = line
        else:
            cur = cur + "\n" + line
    if cur:
        chunks.append(cur)
    return chunks


def _parse_json_loose(content: str) -> dict:
    text = content.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    return json.loads(text)


# ---------------------------------------------------------------- 报告生成


def generate_report(segments: list[dict], stats: dict, meta: dict,
                    ctx: dict, focus: dict | None = None) -> dict:
    """生成教评报告（teaching-eval 内容结构）。"""
    transcript = "\n".join(
        f"[{s['person_name']} {s['start_sec']}-{s['end_sec']}s] {s['text']}"
        for s in segments
    )
    if not transcript:
        return {"content_text": "未检测到有效语音，请检查录音设备",
                "knowledge_text": "", "summary": "无有效转写",
                "learning_progress": {"overall": 0, "topics": []},
                "error_analysis": [], "knowledge_mastery": [],
                "time_allocation": []}

    payload = {"meta": json.dumps(meta, ensure_ascii=False),
               "stats": json.dumps(stats, ensure_ascii=False)}
    chunks = _chunk(transcript)
    tpl = get_prompt("teaching_eval", PROMPT_TEMPLATE)   # 每门课现读，热生效

    if len(chunks) == 1:
        content = _llm_chat([{
            "role": "user",
            "content": render(tpl, {**payload, "transcript": transcript}),
        }], ctx)
    else:
        chunk_tpl = get_prompt("chunk_summary", CHUNK_PROMPT)
        partials = []
        for c in chunks:
            partials.append(_llm_chat([{
                "role": "user",
                "content": render(chunk_tpl, {"chunk": c}),
            }], ctx))
        content = _llm_chat([{
            "role": "user",
            "content": render(tpl, {**payload,
                                    "transcript": "\n---分片摘要---\n" + "\n".join(partials)}),
        }], ctx)

    try:
        return _parse_json_loose(content)
    except json.JSONDecodeError:
        log.warning("大模型输出非 JSON，已降级为原始文本")
        return {"content_text": content, "knowledge_text": "", "summary": "",
                "learning_progress": {"overall": 0, "topics": []},
                "error_analysis": [], "knowledge_mastery": [],
                "time_allocation": [], "raw": content}


# ---------------------------------------------------------------- 文字版（teaching-eval 格式）


def _student_course(meta: dict) -> tuple[str, str, str]:
    """(学生名, 课程类型, 日期YYYYMMDD)。"""
    student = meta.get("student_name") or "学生"
    course = meta.get("course_type") or meta.get("course_id", "课程")
    date_str = (meta.get("start") or "")[:10].replace("-", "")
    return student, course, date_str


def eval_txt_filename(meta: dict) -> str:
    student, course, date_str = _student_course(meta)
    safe = re.sub(r'[\\/:*?"<>|\s]+', "-", f"{student}_{course}")
    return f"教评报告_{date_str}_{safe}.txt"


def write_eval_txt(report: dict, meta: dict) -> str:
    """纯文字版报告（无 Markdown 符号，只含上课内容 + 知识小结，可直接发微信）。"""
    student, course, date_str = _student_course(meta)
    try:
        y, m, d = date_str[:4], int(date_str[4:6]), int(date_str[6:8])
        date_cn = f"{y}年{m}月{d}日"
    except Exception:  # noqa: BLE001
        date_cn = date_str
    tpl = get_prompt("eval_txt", DEFAULTS["eval_txt"]["text"])
    return render(tpl, {
        "date_cn": date_cn, "student": student, "course": course,
        "content_text": (report.get("content_text") or "").strip() or "（无）",
        "knowledge_text": (report.get("knowledge_text") or "").strip() or "（无）",
    })


# ---------------------------------------------------------------- 可视化 PDF


def build_pdf_data(report: dict, meta: dict, focus: dict | None) -> dict:
    """按 teaching-eval generate_pdf.py 的数据契约组装 data.json。

    engagement 四项优先取专注力引擎真实计算值；缺失时退化为大模型/默认。
    """
    student, course, date_str = _student_course(meta)
    date_iso = (meta.get("start") or "")[:10]
    duration_min = None
    try:
        duration_min = round((meta.get("duration_sec") or 0) / 60) or None
    except Exception:  # noqa: BLE001
        pass
    if not duration_min and focus:
        duration_min = int(round(focus.get("duration_min") or 0)) or None

    alloc = []
    for i, ph in enumerate(report.get("time_allocation") or []):
        alloc.append({"phase": ph.get("phase", f"环节{i + 1}"),
                      "minutes": int(ph.get("minutes", 0)),
                      "color": _PHASE_COLORS[i % len(_PHASE_COLORS)]})

    focus = focus or {}
    engagement = {
        "speaking_count": focus.get("s_turns") or 0,
        "question_count": focus.get("n_question") or 0,
        "focus_score": focus.get("focus_score")
                      or (report.get("learning_progress") or {}).get("overall", 0),
        "participation_rate": int(round(focus.get("s_ratio") or 0)),
    }

    return {
        "date": date_iso,
        "student_name": student,
        "grade": meta.get("grade") or "",
        "course_type": course,
        "duration": f"{duration_min}分钟" if duration_min else "",
        "location": meta.get("location") or "",
        "teach_type": meta.get("teach_type") or "",
        "learning_progress": report.get("learning_progress") or {"overall": 0, "topics": []},
        "engagement": engagement,
        "error_analysis": report.get("error_analysis") or [],
        "knowledge_mastery": report.get("knowledge_mastery") or [],
        "time_allocation": alloc,
        "summary": (report.get("summary") or "")[:300],
    }


def render_pdf(pdf_data: dict, out_path: Path) -> Path | None:
    """调用 teaching-eval 的 generate_pdf.py 渲染 PDF；失败返回 None（不中断流程）。"""
    gen = Path(_PDF_GENERATOR)
    if not gen.exists():
        log.warning("PDF 生成器不存在：%s（跳过 PDF）", gen)
        return None
    data_file = out_path.with_suffix(".data.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data_file.write_text(json.dumps(pdf_data, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    try:
        r = subprocess.run(
            [sys.executable, str(gen), "--data", str(data_file),
             "--output", str(out_path)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=300,
            creationflags=(subprocess.CREATE_NO_WINDOW
                           if os.name == "nt" else 0))
        if r.returncode != 0 or not out_path.exists():
            log.warning("PDF 渲染失败（保留 data.json 可重跑）: %s",
                        (r.stderr or r.stdout).strip()[:300])
            return None
        log.info("教评 PDF 已生成: %s", out_path)
        return out_path
    except Exception as e:  # noqa: BLE001
        log.warning("PDF 渲染异常（跳过）: %s", e)
        return None


# ---------------------------------------------------------------- 推送


def push_report(report: dict, meta: dict, ctx: dict,
                stats: dict | None = None,
                files: dict[str, Path] | None = None) -> dict:
    """产物回传 COS + 可选飞书 webhook 推送。

    files: {逻辑名: 本地路径}（教评 txt/pdf、专注力 html/xlsx、focus_data.json 等），
    统一上传到 {result_prefix}/{date}/ 下，文件名保持不变。
    返回: {逻辑名: cos_key}
    """
    store = ctx["store"]
    cfg = ctx["config"].get("report", {})
    prefix = cfg.get("result_prefix", "jiaopei/reports")
    date_str = (meta.get("start") or "")[:10].replace("-", "")
    course_id = meta.get("course_id", "unknown")

    keys: dict[str, str] = {}
    # 结构化数据（报告 + 统计）始终回传，供复核与复算
    json_key = f"{prefix}/{date_str}/{course_id}_report.json"
    store.put_json(json_key, {"meta": meta, "stats": stats or {}, "report": report})
    keys["json_key"] = json_key

    for name, path in (files or {}).items():
        p = Path(path)
        if not p.exists():
            continue
        key = f"{prefix}/{date_str}/{p.name}"
        store.put_file(key, str(p))
        keys[name] = key
        log.info("产物已回传 COS: %s", key)

    webhook = cfg.get("feishu_webhook") or os.getenv("FEISHU_WEBHOOK", "")
    if webhook:
        try:
            student, course, _ = _student_course(meta)
            requests.post(webhook, json={
                "msg_type": "text",
                "content": {"text": f"课程 {student} {course} 教评完成，详见 COS {prefix}/{date_str}/"},
            }, timeout=10)
        except Exception as e:  # noqa: BLE001
            log.warning("飞书推送失败（不影响报告）: %s", e)
    return keys
