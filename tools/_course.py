# -*- coding: utf-8 -*-
"""定位本机课堂数据目录（tools/ 下离线诊断脚本共用）。

为什么不把目录名写进代码：课程目录形如「<学生姓名>-<科目>」，写死会把真实
学生姓名带进公开仓库。

映射来源（按优先级）：
  1) tools/courses.local.json —— 本机私有映射，已在 .gitignore 中排除。格式：
       {
         "main":  "C:\\\\recorder\\\\cloud_work\\\\<主课程目录>",
         "cross": "C:\\\\recorder\\\\cloud_work\\\\<跨课程验证用目录>"
       }
  2) 环境变量 REC_COURSE_DIRS —— 分号分隔，按出现顺序对应 main、cross。
  3) 两者都没有 → 返回占位路径；脚本判存在性后会自行跳过。

注意：这些脚本依赖的课堂音频（cloud_work/）与声纹库未随仓库发布，
      换机后需自备数据，或先设好上面的映射。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
ROOT = Path(os.getenv("REC_ROOT", str(TOOLS_DIR.parent)))
CLOUD_WORK = ROOT / "cloud_work"
LOCAL_CONF = TOOLS_DIR / "courses.local.json"

_KEYS = ("main", "cross")
_cache: dict | None = None


def _from_env() -> dict:
    raw = (os.getenv("REC_COURSE_DIRS") or "").strip()
    if not raw:
        return {}
    parts = [p.strip() for p in raw.split(";") if p.strip()]
    return dict(zip(_KEYS, parts))


def _load() -> dict:
    m: dict = {}
    if LOCAL_CONF.exists():
        try:
            data = json.loads(LOCAL_CONF.read_text(encoding="utf-8"))
            m.update({k: v for k, v in data.items()
                      if isinstance(v, str) and v.strip()})
        except (OSError, ValueError) as e:      # noqa: BLE001
            print(f"[警告] {LOCAL_CONF.name} 解析失败：{e}", file=sys.stderr)
    for k, v in _from_env().items():
        m.setdefault(k, v)                      # 文件优先，环境变量兜底
    return m


def mapping() -> dict:
    """返回 {key: 绝对路径字符串}。"""
    global _cache
    if _cache is None:
        _cache = _load()
    return _cache


def course(key: str = "main") -> Path:
    """第 key 个课堂数据目录；未配置时返回不存在的占位路径。"""
    v = mapping().get(key)
    return Path(v) if v else CLOUD_WORK / f"__unconfigured_{key}__"


def enroll_samples_dir() -> Path:
    """近场注册素材目录（cloud_work/enroll_samples）。"""
    return CLOUD_WORK / "enroll_samples"


def enroll_sample(pattern: str = "*.wav") -> Path | None:
    """enroll_samples 下第一个匹配 pattern 的 wav；无则 None。"""
    d = enroll_samples_dir()
    if not d.is_dir():
        return None
    hits = sorted(d.glob(pattern))
    return hits[0] if hits else None


def person(key: str = "teacher") -> str:
    """本机人员姓名（key 如 'teacher' / 'S002'）。

    姓名是个人数据，不写进源码；缺失时回落为 key 本身，脚本仍可运行。
    """
    return str(mapping().get(f"name_{key}") or key)


def note_missing(key: str = "main") -> None:
    """未配置映射时打印一次可操作的提示（不抛异常）。"""
    if not mapping().get(key):
        print(f"[提示] 未配置课程目录 {key!r}。请在 {LOCAL_CONF} 写入本机路径，"
              f"或设置环境变量 REC_COURSE_DIRS='<main>;<cross>'。",
              file=sys.stderr)
