# -*- coding: utf-8 -*-
"""飞书总日程表课表匹配（方案B · 课表层）。

两个用途：
1. sync_lessons_file()：把飞书课表同步成本地 lessons.json（recorder 每秒读取，
   实现按真实课表自动开始/结束录制，无需手工维护课表文件）。
2. match_lesson()：报告生成前，按录制时间窗口反查课表，拿到学生姓名 / 课程类型 /
   授课类型 / 上课地点 / 年级，用于教评报告与专注力报告的抬头信息。
   对手动录制、VAD 触发录制同样有效。

数据源：飞书 Base「总日程表」
  base  Xb34bXSVRaQk7ysfAQScPZp5nPh
  table tblWoTVtU4lqJoes（课程安排主表）/ tblQGkH5DweXpOW8（学员信息表）

调用通道：lark-cli（node 直调，规避 Git Bash 路径问题）。
任何一步失败都只记日志并返回「未匹配」，绝不影响录制与报告主流程。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("schedule")

# 课程类型中含以下词 → 视为会议（而非 1 对 1 / 班课），走会议纪要而非教评。
# 实证：9/8 汤朔日程「10:00-12:00｜｜例会｜」course_type=例会、无学生、无授课类型。
MEETING_KEYWORDS = ("会议", "例会", "周会", "晨会", "夕会", "研讨会",
                    "培训会", "家长会", "备课会", "教研", "筹备")

# ---------------------------------------------------------------- 运行时定位
# 安装包把 Node 运行时与 lark-cli 捆绑在 <root>\vendor\node 下；开发机则用
# .workbuddy 托管目录。旧版把版本号写死成 22.22.2-2，该目录被清理后同步就
# 静默失败（lessons.json 停在 9/23 即此因），故改为「按存在性挑选」。
_ROOT_DIR = Path(__file__).resolve().parent.parent


def _pick(*cands: Path) -> str:
    """返回第一个存在的候选；都不存在时返回第一个（供报错信息使用）。"""
    for c in cands:
        if c.exists():
            return str(c)
    return str(cands[0])


def _dev_node_candidates() -> list[Path]:
    """开发机托管 Node：优先 22.x（本项目验证过的主版本），其次取最新。"""
    base = Path(r"C:/Users/汤朔/.workbuddy/binaries/node/versions")
    if not base.is_dir():
        return []
    try:
        found = sorted((d / "node.exe" for d in base.iterdir() if d.is_dir()),
                       key=lambda p: p.parent.name, reverse=True)
    except OSError:
        return []
    return ([p for p in found if p.parent.name.startswith("22.")]
            + [p for p in found if not p.parent.name.startswith("22.")])


_BUNDLED_NODE = _ROOT_DIR / "vendor" / "node" / "node.exe"
_BUNDLED_LARK = _ROOT_DIR / "vendor" / "node" / "lark-cli" / "scripts" / "run.js"
_DEV_LARK = Path(r"C:/Users/汤朔/.workbuddy/binaries/node/cli-connector-packages"
                 r"/node_modules/@larksuite/cli/scripts/run.js")

NODE_PATH_RESOLVED = _pick(_BUNDLED_NODE, *_dev_node_candidates())
LARK_CLI_RESOLVED = _pick(_BUNDLED_LARK, _DEV_LARK)

# 默认配置（可被 cloud_config.json 的 feishu 节覆盖）
DEFAULTS = {
    "enabled": True,
    "base_token": "Xb34bXSVRaQk7ysfAQScPZp5nPh",
    "schedule_table": "tblWoTVtU4lqJoes",
    "student_table": "tblQGkH5DweXpOW8",
    "teacher_name": "汤朔",        # 兼容保留（单值）；被 teacher_names 覆盖
    "teacher_names": [],          # 录制触发覆盖的教师名单（列表优先）
    "node_path": NODE_PATH_RESOLVED,
    "lark_cli": LARK_CLI_RESOLVED,
    "match_tolerance_min": 60,      # 录制与课表时间窗的最大允许偏差
    "sync_min_interval_sec": 600,   # lessons.json 最小同步间隔
}

_cfg: dict = {}
_last_sync: float = 0.0


def _conf(ctx: dict | None = None) -> dict:
    cfg = dict(DEFAULTS)
    if ctx:
        cfg.update((ctx.get("config") or {}).get("feishu", {}) or {})
    else:
        cfg.update(_cfg)
    return cfg


def configure(feishu_cfg: dict) -> None:
    """供 auto_pipeline 启动时注入 cloud_config.json 的 feishu 节。"""
    global _cfg
    _cfg = dict(feishu_cfg or {})


# ---------------------------------------------------------------- lark-cli 通道


def _lark(args: list[str], cfg: dict, timeout: int = 60) -> dict:
    """调用 lark-cli 并解析 JSON 输出；失败抛异常由调用方降级。

    注意：record-list 的 --json 是布尔开关，而 record-get 的 --json
    是字符串参数（接收 record_id_list）。因此 JSON 输出标志由各调用方
    自行附加，这里只统一补 --as user。
    """
    cmd = [cfg["node_path"], cfg["lark_cli"], *args, "--as", "user"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       creationflags=(subprocess.CREATE_NO_WINDOW
                                      if os.name == "nt" else 0))
    if r.returncode != 0:
        raise RuntimeError(f"lark-cli 退出码 {r.returncode}: {r.stderr.strip()[:200]}")
    out = json.loads(r.stdout)
    if not out.get("ok"):
        raise RuntimeError(f"lark-cli 返回失败: {str(out)[:200]}")
    return out["data"]


def _records_by_name(data: dict) -> list[dict]:
    """record-list 返回的 data.data 是二维数组，用 data.fields 列序映射成 dict 列表。"""
    fields = data.get("fields") or []
    rows = data.get("data") or []
    return [dict(zip(fields, row)) for row in rows]


def _first(value):
    """select/link 等字段返回 [值] 或 [{'id':..}]，取标量。"""
    if isinstance(value, list):
        if not value:
            return None
        v = value[0]
        return v.get("id") if isinstance(v, dict) else v
    return value


# ---------------------------------------------------------------- 课表查询


def _naive(dt: datetime) -> datetime:
    """统一为本地时区的 naive datetime，避免 naive/aware 比较报错。"""
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


def fetch_day_lessons(day: datetime, cfg: dict,
                      teacher_name: str | None = None) -> list[dict]:
    """拉取指定日期（±1 天缓冲）的课表记录，解析为统一结构。

    teacher_name：指定教师姓名 → 只拉该老师的课；
                  为 None / 空 → 拉当日全部课程（不按老师过滤），
                  供「声纹识别出教师后按姓名匹配，或未识别时按当日全部比对」。
    """
    d0 = (day - timedelta(days=1)).strftime("%Y-%m-%d")
    d1 = (day + timedelta(days=1)).strftime("%Y-%m-%d")
    conditions = [
        ["上课时间", ">", f"ExactDate({d0})"],
        ["上课时间", "<", f"ExactDate({d1})"],
    ]
    teacher_hint = (teacher_name or "").strip()
    if teacher_hint:
        conditions.insert(0, ["老师", "==", teacher_hint])
    filt = json.dumps({"logic": "and", "conditions": conditions}, ensure_ascii=False)
    data = _lark(["base", "+record-list",
                  "--base-token", cfg["base_token"],
                  "--table-id", cfg["schedule_table"],
                  "--filter-json", filt,
                  "--sort-json", '[{"field":"上课时间","desc":false}]',
                  "--limit", "100", "--json"], cfg)
    lessons = []
    for rec in _records_by_name(data):
        try:
            start = _naive(datetime.fromisoformat(str(rec.get("上课时间"))))
            end = _naive(datetime.fromisoformat(str(rec.get("下课时间"))))
        except (TypeError, ValueError):
            continue
        label = str(rec.get("老师课程ID") or "")
        student_name, course_label = _parse_label(label)
        course_type = _first(rec.get("课程类型")) or ""
        teach_type = _first(rec.get("授课类型")) or ""
        lessons.append({
            "start": start, "end": end,
            "teacher_name": _first(rec.get("老师")) or "",
            "course_type": course_type,
            "teach_type": teach_type,
            "location": _first(rec.get("上课地点")) or "",
            "student_record_id": _first(rec.get("学生")),
            "student_name": student_name,
            "label": label,
            "course_label": course_label,
            "duration_hours": str(rec.get("课程时长") or ""),
            # 会议判定：课程类型含会议词，或无学生且无授课类型（仅时间/无课程内容）
            "is_meeting": _looks_like_meeting(
                course_type=course_type, student_name=student_name,
                teach_type=teach_type),
        })
    return lessons


def _looks_like_meeting(course_type: str = "", student_name: str = "",
                        teach_type: str = "") -> bool:
    """判定该日程是否为会议而非 1 对 1 / 班课。

    规则（实证自 9/8「10:00-12:00｜｜例会｜」course_type=例会/无学生/无授课）：
      1. 课程类型含会议关键词 → 会议
      2. 课程类型为空 且 无学生名 且 无授课类型 → 会议（占位/未填课程）
      否则视为上课。
    """
    ct = (course_type or "").strip()
    if any(kw in ct for kw in MEETING_KEYWORDS):
        return True
    if not ct and not (student_name or "").strip() and not (teach_type or "").strip():
        return True
    return False


def _parse_label(label: str) -> tuple[str, str]:
    """老师课程ID「13:30-15:30｜Lucas Zhang｜IG物理｜滨江」→ (学生名, 课程标签)。
    分隔符兼容全角｜(U+FF5C) 与盒线│(U+2502)。"""
    parts = [p.strip() for p in re.split(r"[｜│]", label) if p.strip()]
    if len(parts) >= 3:
        return parts[1], f"{parts[1]} · {parts[2]}"
    return "", label


_GRADE_MAP = {1: "一年级", 2: "二年级", 3: "三年级", 4: "四年级", 5: "五年级",
              6: "六年级", 7: "初一", 8: "初二", 9: "初三",
              10: "高一", 11: "高二", 12: "高三"}


def fetch_student_grade(student_record_id: str, cfg: dict) -> str:
    """回查学员表「当前学员年级」。失败返回空串。"""
    if not student_record_id:
        return ""
    try:
        data = _lark(["base", "+record-get",
                      "--base-token", cfg["base_token"],
                      "--table-id", cfg["student_table"],
                      "--record-id", student_record_id,
                      "--format", "json"], cfg)
        rows = _records_by_name(data)
        fields = rows[0] if rows else {}
        grade = fields.get("当前学员年级")
        if isinstance(grade, list):  # formula 可能返回包装
            grade = grade[0] if grade else None
        try:
            return _GRADE_MAP.get(int(float(str(grade))), str(grade or ""))
        except (TypeError, ValueError):
            return str(grade or "")
    except Exception as e:  # noqa: BLE001
        log.warning("回查学员年级失败 %s: %s", student_record_id, e)
        return ""


# ---------------------------------------------------------------- 匹配


def match_lesson(rec_start: datetime, rec_end: datetime,
                 ctx: dict | None = None,
                 teacher_hint: str | None = None) -> dict:
    """按录制时间窗反查课表，返回匹配到的课程信息。

    参数：
      teacher_hint —— 声纹识别出的授课教师姓名（role=teacher 的说话人）。
                      传值 → 只在该教师的当日课程里按时间窗匹配；
                      None/空 且 teacher_mandatory=True → 不猜测（走仅存档）。
      teacher_mandatory —— 是否强制要求教师身份。True（默认，对应"强制声纹教师"
                      策略）时，无教师身份不匹配；False 时退化为当日全部课程里
                      按时间窗取重叠最大（供旧调用方/纯时间兜底）。

    返回 key：matched / is_meeting / teacher_name / reason 等。
    """
    cfg = _conf(ctx)
    if not cfg.get("enabled"):
        return {"matched": False, "reason": "disabled"}
    rec_start, rec_end = _naive(rec_start), _naive(rec_end)

    teacher = (teacher_hint or "").strip()
    if not teacher:
        # 未识别出教师。默认不猜（仅存档），由调用方显式放行时才拉全部。
        log.info("无教师身份（声纹未命中），课表匹配放行：rec %s ~ %s",
                 rec_start, rec_end)
        return {"matched": False, "reason": "no_teacher",
                "teacher_name": "", "is_meeting": None}
    try:
        lessons = fetch_day_lessons(rec_start, cfg, teacher_name=teacher)
    except Exception as e:  # noqa: BLE001
        log.warning("飞书课表查询失败（跳过匹配）: %s", e)
        return {"matched": False, "reason": "query_failed"}

    tol = timedelta(minutes=int(cfg.get("match_tolerance_min", 60)))
    best, best_overlap = None, 0.0
    for ls in lessons:
        # 时间窗交集秒数（两端各放 tolerance）
        lo = max(rec_start, ls["start"] - tol)
        hi = min(rec_end, ls["end"] + tol)
        overlap = (hi - lo).total_seconds()
        if overlap > best_overlap:
            best, best_overlap = ls, overlap
    if not best or best_overlap <= 0:
        log.info("课表未匹配：%s ~ %s，教师 %s 当天无对应课程",
                 rec_start, rec_end, teacher)
        return {"matched": False, "reason": "no_slot",
                "teacher_name": teacher, "is_meeting": None}
    grade = fetch_student_grade(best.get("student_record_id"), cfg)
    log.info("课表匹配成功：%s %s %s（重叠 %.0f 分钟）",
             best["student_name"], best["course_type"], best["label"],
             best_overlap / 60)
    return {
        "matched": True,
        "is_meeting": bool(best.get("is_meeting")),
        "teacher_name": best.get("teacher_name") or teacher,
        "student_name": best["student_name"],
        "course_type": best["course_type"],
        "teach_type": best["teach_type"],
        "location": best["location"],
        "grade": grade,
        "course_label": best["course_label"] or best["label"],
        "schedule_start": best["start"].isoformat(),
        "schedule_end": best["end"].isoformat(),
        "schedule_hours": best["duration_hours"],
    }


# ---------------------------------------------------------------- lessons.json 同步


def _safe_course_id(student: str, course_type: str, start: datetime) -> str:
    raw = f"{student}-{course_type}" if student else f"课{start.strftime('%H%M')}"
    return re.sub(r'[\\/:*?"<>|\s｜│]+', "-", raw).strip("-") or "课程"


def _teacher_list(cfg: dict) -> list[str]:
    """录制触发需要覆盖的教师名单。

    `teacher_names`（列表）优先；为空时回落单值 `teacher_name`。
    去重保序、剔除空值。返回空列表表示无法确定覆盖范围。
    """
    raw = cfg.get("teacher_names")
    names: list[str] = []
    if isinstance(raw, (list, tuple)):
        names = [str(x).strip() for x in raw if str(x).strip()]
    elif isinstance(raw, str) and raw.strip():
        names = [raw.strip()]
    if not names:
        single = (cfg.get("teacher_name") or "").strip()
        if single:
            names = [single]
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def sync_lessons_file(lessons_path: Path, ctx: dict | None = None,
                      force: bool = False) -> bool:
    """把今明两天的飞书课表写入本地 lessons.json（recorder 逐秒读取）。

    覆盖范围 = 配置的教师名单 `teacher_names`（回落单值 `teacher_name`）。
    飞书 filter 不支持嵌套 OR（and 内不能再放 or 组），故按教师逐个查询后
    合并、按 (开始时间, 课程标签) 去重——同一节课挂多名教师时只留一条。

    频率限制：默认 10 分钟内不重复同步（force=True 跳过）。
    全部教师查询均失败时保留旧文件。返回是否发生了写入。
    """
    global _last_sync
    cfg = _conf(ctx)
    if not cfg.get("enabled"):
        return False
    now = time.time()
    if not force and now - _last_sync < int(cfg.get("sync_min_interval_sec", 600)):
        return False
    _last_sync = now

    teachers = _teacher_list(cfg)
    if not teachers:
        log.warning("未配置 teacher_names / teacher_name，跳过课表同步（保留旧文件）")
        return False

    merged: list[dict] = []
    seen: set[tuple] = set()
    ok_count = 0
    for t in teachers:
        try:
            part = fetch_day_lessons(datetime.now(), cfg, teacher_name=t)
            ok_count += 1
        except Exception as e:  # noqa: BLE001
            log.warning("课表同步失败（教师 %s，跳过该教师）: %s", t, e)
            continue
        for ls in part:
            key = (ls["start"], ls["label"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(ls)
    if ok_count == 0:
        log.warning("课表同步失败（全部教师查询异常，保留旧 lessons.json）")
        return False

    items = []
    for ls in merged:
        if ls["start"].date() < datetime.now().date():
            continue  # 只写今天及以后
        items.append({
            "course_id": _safe_course_id(ls["student_name"], ls["course_type"], ls["start"]),
            "teacher": ls["teacher_name"] or teachers[0],  # 写真实归属教师
            "start": ls["start"].isoformat(),
            "end": ls["end"].isoformat(),
            "room": "room-01",
            "student": ls["student_name"],
            "course_type": ls["course_type"],
            "location": ls["location"],
        })
    items.sort(key=lambda x: x["start"])
    lessons_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = lessons_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(lessons_path)
    log.info("课表已同步：教师 %s → %d 节 → %s",
             "/".join(teachers), len(items), lessons_path)
    return True
