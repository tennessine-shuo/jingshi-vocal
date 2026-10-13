# -*- coding: utf-8 -*-
"""云端处理流水线编排（方案B · 云端 · 正式版 v3）。

顺序：读取元数据 → 转码+VAD → ASR(含说话人分离) → 角色判定（启发式）
     → 语义清洗 → 飞书课表匹配（学生/课程/地点/年级 enrichment）
     → 特征统计 → 专注力引擎（规则计算，不用大模型）
     → 大模型教评（teaching-eval 内容结构：上课内容+知识小结+PDF数据）
     → 本地产出（教评 txt/pdf + 专注力 html/xlsx + focus_data.json）
     → 回传 COS + 推送。
每步最多重试 2 次（指数退避）；课表匹配 / PDF 渲染失败只降级不中断。

v3（2026-10-13）：**取消声纹比对环节**。教师归属改为采信 meta.teacher
（录制端由控制台下拉栏手动选定，或课表触发的真实教师），说话人角色由
启发式判定（发言总时长最长者 = 教师）。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from cos_client import CosStore
from focus import (compute_focus, render_focus_html, update_focus_entry_xlsx,
                   write_focus_report_xlsx)
from meeting_summary import (build_meeting, meeting_filename,
                             push_meeting, render_meeting_html)
from report import (build_pdf_data, eval_txt_filename, generate_report,
                    push_report, render_pdf, write_eval_txt)
from schedule_feishu import match_lesson
from stages import (asr_transcribe, clean_segments, compute_stats, diarize,
                    heuristic_roles, transcode_and_vad)

log = logging.getLogger("pipeline")

MAX_RETRY = 2


@dataclass
class Task:
    bucket: str
    meta_key: str
    meta: dict = field(default_factory=dict)
    segments: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    focus: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)
    files: dict = field(default_factory=dict)   # 逻辑名 -> 本地路径
    status: str = "running"


def _retry(name: str, fn, *args):
    """带指数退避的重试包装。"""
    for i in range(MAX_RETRY + 1):
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001
            log.warning("%s 第 %d 次失败: %s", name, i + 1, e)
            if i == MAX_RETRY:
                raise
            time.sleep(2 ** i)
    return None


def _soft(name: str, fn, *args):
    """允许失败的环节：只记日志，返回 None，不中断流水线。"""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        log.warning("%s 失败（已跳过，不影响主流程）: %s", name, e)
        return None


class Pipeline:
    def __init__(self, config: dict):
        self.config = config
        cos_cfg = config["cos"]
        self.store = CosStore(bucket=cos_cfg["bucket"], region=cos_cfg["region"],
                              secret_id=cos_cfg.get("secret_id"),
                              secret_key=cos_cfg.get("secret_key"))

    def _new_ctx(self, course_id: str) -> dict:
        workdir = Path(self.config.get("workdir", "/tmp/cloud_work")) / course_id
        workdir.mkdir(parents=True, exist_ok=True)
        return {"store": self.store, "config": self.config,
                "workdir": str(workdir)}

    def _local_outdir(self, meta: dict) -> Path:
        base = Path(self.config.get("report", {}).get(
            "local_dir", r"C:/recorder/reports"))
        date_str = (meta.get("start") or "")[:10].replace("-", "") or "unknown"
        out = base / date_str
        out.mkdir(parents=True, exist_ok=True)
        return out

    # ---------------- 课表匹配（enrichment） ----------------

    def _enrich_meta(self, meta: dict) -> dict:
        """用飞书总日程表补全课程信息；无法匹配（无教师/无日程）则标记原因。

        教师取 meta.teacher（录制端手动选定或课表自带），作为 teacher_hint
        传给课表匹配；命中即区分「上课 / 会议」；均未命中则由调用方走
        「仅存档」。云端不做声纹识别 —— meta.teacher 缺失或为「未知」时
        直接走仅存档，不再猜测。
        """
        meta["schedule_matched"] = False
        meta["schedule_reason"] = "unknown"
        meta["is_meeting"] = None
        try:
            start = datetime.fromisoformat(meta["start"])
            end = datetime.fromisoformat(meta["end"])
        except (KeyError, ValueError):
            meta["schedule_reason"] = "bad_time"
            meta.setdefault("course_label", meta.get("course_id", "未知课程"))
            return meta
        teacher_hint = (meta.get("teacher") or "").strip()
        if teacher_hint in ("", "未知"):
            teacher_hint = ""
            meta["schedule_reason"] = "no_teacher"
            meta.setdefault("course_label", meta.get("course_id", "未知课程"))
            log.info("meta 无有效教师（%r），跳过课表匹配 → 仅存档",
                     meta.get("teacher"))
            return meta
        hit = match_lesson(start, end, ctx={"config": self.config},
                           teacher_hint=teacher_hint)
        if hit.get("matched"):
            meta.update({
                "student_name": hit["student_name"],
                "course_type": hit["course_type"] or meta.get("course_type", ""),
                "teach_type": hit["teach_type"],
                "location": hit["location"],
                "grade": hit["grade"],
                "course_label": hit["course_label"],
                "is_meeting": bool(hit.get("is_meeting")),
                "schedule_matched": True,
                "schedule_reason": "matched",
                "schedule_hours": hit.get("schedule_hours", ""),
            })
        else:
            meta["schedule_reason"] = hit.get("reason", "no_match")
            # 无法匹配时尽量保留原元信息作为可读标签（仅存档用）
            meta.setdefault("course_label", meta.get("course_id", "未知课程"))
        return meta

    # ---------------- 主流程 ----------------

    def _safe_label(self, meta: dict) -> str:
        return re.sub(r'[\\/:*?"<>|\s]+', "-",
                      meta.get("course_label") or meta.get("course_id") or "课程")

    def _make_task(self, store, meta_key: str, meta: dict) -> Task:
        """构造 Task（供 process / reprocess_course 共用）。"""
        return Task(bucket=getattr(store, "bucket", ""),
                    meta_key=meta_key, meta=meta)

    def _archive_transcript(self, task: Task, speakers: list[dict],
                            date_str: str) -> dict:
        """仅存档：meta 无有效教师或课表无匹配日程时，只保留结构化转写与统计。"""
        out = self._local_outdir(task.meta)
        label = self._safe_label(task.meta)
        tjson = out / f"archived_{date_str}_{label}_transcript.json"
        tjson.write_text(json.dumps({
            "meta": task.meta, "segments": task.segments,
            "stats": task.stats, "speakers": speakers,
            "archived_reason": task.meta.get("schedule_reason", "unknown"),
            "archived_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        task.files["archive_json"] = tjson
        # 仅回传结构化转写（便于人工补跑），不生成任何报告
        prefix = self.config.get("report", {}).get("result_prefix", "jiaopei/reports")
        key = f"{prefix}/{date_str}/{task.meta.get('course_id')}_transcript.json"
        self.store.put_json(key, {
            "course_id": task.meta.get("course_id"),
            "meta": task.meta, "segments": task.segments,
            "stats": task.stats, "speakers": speakers,
            "archived_reason": task.meta.get("schedule_reason"),
        })
        task.status = "archived"
        log.info("仅存档（未确定上课/会议，%s）: %s",
                 task.meta.get("schedule_reason"), key)
        return {"transcript_key": key}

    def _produce_lesson(self, task: Task, speakers: list[dict],
                        date_str: str, ctx: dict) -> dict:
        """上课分支：专注力 + 教评 + 可视化 PDF + 回传。"""
        out = self._local_outdir(task.meta)
        label = self._safe_label(task.meta)

        # 专注力引擎（规则计算，软失败——失败则跳过专注力产物）
        task.focus = _soft("专注力计算", compute_focus,
                           task.segments, task.meta, ctx) or {}
        task.report = _retry("报告生成", generate_report,
                             task.segments, task.stats, task.meta, ctx,
                             task.focus or None)

        txt_path = out / eval_txt_filename(task.meta)
        txt_path.write_text(write_eval_txt(task.report, task.meta), encoding="utf-8")
        task.files["eval_txt"] = txt_path

        pdf_data = build_pdf_data(task.report, task.meta, task.focus)
        pdf_path = _soft("PDF渲染", render_pdf, pdf_data,
                         out / txt_path.name.replace(".txt", ".pdf"))
        if pdf_path:
            task.files["eval_pdf"] = pdf_path

        if task.focus:
            fjson = out / f"focus_data_{date_str}_{label}.json"
            fjson.write_text(json.dumps(task.focus, ensure_ascii=False, indent=1),
                             encoding="utf-8")
            task.files["focus_json"] = fjson
            p = _soft("专注力HTML", render_focus_html, task.focus,
                      out / f"专注力报告_{date_str}_{label}.html")
            if p:
                task.files["focus_html"] = p
            p = _soft("专注力报告xlsx", write_focus_report_xlsx, task.focus,
                      out / f"专注力报告_{date_str}_{label}.xlsx")
            if p:
                task.files["focus_xlsx"] = p
            master = Path(self.config.get("report", {}).get(
                "focus_entry_file", r"C:/recorder/reports/专注力数据录入表.xlsx"))
            _soft("专注力录入表追加", update_focus_entry_xlsx, task.focus, master)

        keys = _retry("报告推送", push_report,
                      task.report, task.meta, ctx, task.stats, task.files)
        task.status = "done"
        return {**keys}

    def _produce_meeting(self, task: Task, speakers: list[dict],
                         date_str: str, ctx: dict) -> dict:
        """会议分支：会议纪要 + 思维导图 HTML（单文件），跳过专注力三件套/教评/PDF。"""
        out = self._local_outdir(task.meta)
        meeting = _retry("会议纪要生成", build_meeting,
                         task.segments, task.meta, ctx) or {
            "title": "会议纪要", "topics": [], "conclusions": [],
            "action_items": [], "speaker_stats": [],
            "summary": "（纪要生成失败）", "meta": task.meta}
        # 发言统计内建于 meeting，单独落一份便于复核
        fjson = out / f"会议纪要_{date_str}_{self._safe_label(task.meta)}.data.json"
        fjson.write_text(json.dumps(meeting, ensure_ascii=False, indent=1),
                         encoding="utf-8")
        task.files["meeting_json"] = fjson
        base = out / meeting_filename(task.meta)
        p = _soft("会议纪要HTML", render_meeting_html, meeting,
                  base.with_suffix(".html"))
        if p:
            task.files["meeting_html"] = p
        keys = _soft("会议纪要推送", push_meeting, meeting, task.meta, ctx,
                     task.files)
        task.status = "meeting_done"
        return {**(keys or {})}

    def _stamp_artifacts(self, task: Task, date_str: str) -> None:
        """给本次产出的成品打「源 meta 指纹」，供 auto_pipeline 判断是否有新录音。

        指纹 = {meta_key: meta.uploaded_at}，采用**累积**写入：同一课程被录成
        多段时（中途重启/换麦），后一段处理完会把先前各段一并记上。轮询侧据此
        判断"成品是否已覆盖当前全部 meta"，从而做到：meta 更新自动重跑、
        历史课程不被误跑。缺失该字段的旧成品一律视为已完成。
        """
        prefix = self.config.get("report", {}).get(
            "result_prefix", "jiaopei/reports")
        base = f"{prefix}/{date_str}/{task.meta.get('course_id')}"
        for suf in ("_report.json", "_meeting.json", "_transcript.json"):
            key = base + suf
            try:
                if not self.store.exists(key):
                    continue
                data = self.store.get_json(key)
                src = dict(data.get("source_metas") or {})
                src[task.meta_key] = task.meta.get("uploaded_at", "")
                data["source_metas"] = src
                data["last_processed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                self.store.put_json(key, data)
                log.info("溯源标记已写入 %s（已覆盖 %d 段录音）", key, len(src))
            except Exception as exc:  # noqa: BLE001
                log.warning("溯源标记写入失败 %s：%s", key, exc)

    def process(self, bucket: str, meta_key: str) -> dict:
        task = Task(bucket=bucket, meta_key=meta_key)
        log.info("任务启动 meta=%s", meta_key)
        ctx: dict = {}
        try:
            task.meta = _retry("读取元数据", self.store.get_json, meta_key)
            ctx = self._new_ctx(task.meta.get("course_id", "unknown"))

            vad = _retry("转码+VAD", transcode_and_vad, task.meta, ctx)
            speakers = _retry("说话人分离", diarize, task.meta, ctx)
            speakers = _retry("角色判定", heuristic_roles, speakers, task.meta, ctx)
            raw = _retry("ASR转写", asr_transcribe, vad, task.meta, ctx)
            task.segments = _retry("语义清洗", clean_segments, raw, speakers)

            # 课表 enrichment：按 meta 里的教师归属课程，判定上课/会议（软失败）
            task.meta = _soft("飞书课表匹配", self._enrich_meta,
                              task.meta) or task.meta
            task.stats = _retry("特征统计", compute_stats,
                                task.segments, task.meta, ctx)
            date_str = (task.meta.get("start") or "")[:10].replace("-", "")

            # ---- 分流：仅存档 / 会议 / 上课 ----
            if not task.meta.get("schedule_matched"):
                # 无有效教师，或该教师当日无匹配日程 → 仅存档（不生成任何报告）
                result = self._archive_transcript(task, speakers, date_str)
            elif task.meta.get("is_meeting"):
                # 匹配到会议日程 → 会议纪要 + 思维导图
                result = self._produce_meeting(task, speakers, date_str, ctx)
            else:
                # 匹配到上课日程 → 教评 + 专注力（原链路）
                result = self._produce_lesson(task, speakers, date_str, ctx)

            # 给成品打「源 meta 指纹」—— 轮询侧据此识别"同一课程的新录音"。
            # 软失败：打标失败只退化为"存在性判定"，不影响报告本身。
            _soft("溯源标记", self._stamp_artifacts, task, date_str)

            task.status = task.status or "done"
            log.info("产物: %s", result)
        except Exception as e:  # noqa: BLE001
            task.status = "failed"
            log.error("任务失败 meta=%s: %s", meta_key, e)
            if ctx:
                try:  # 失败状态落 COS，便于人工发现与重跑
                    fail_key = meta_key.replace("meta.json", "pipeline_failed.json")
                    ctx["store"].put_json(fail_key, {
                        "meta_key": meta_key, "error": str(e),
                        "failed_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
                except Exception:  # noqa: BLE001
                    pass
        log.info("任务结束 course=%s status=%s",
                 task.meta.get("course_id"), task.status)
        return {"status": task.status, "report": task.report,
                "stats": task.stats, "focus": task.focus,
                "files": {k: str(v) for k, v in task.files.items()}}
