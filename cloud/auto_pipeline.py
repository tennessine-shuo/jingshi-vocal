# -*- coding: utf-8 -*-
"""自动教评流水线守护进程（方案B · 本地常驻）。

替代 SCF 云函数的本地方案：轮询 COS，发现新课程（meta.json 到达）
即自动运行完整教评流水线（转码 → ASR → 声纹 → 统计 → hy3 报告 → 回传）。

为什么不用 SCF：
  - 转码依赖 ffmpeg，SCF 标准运行时没有（需额外 Layer）
  - SCF 事件函数最长 900s，2 小时课程的 ASR 轮询可能超时
  - 本机常驻零额外配置、零云函数费用

跳过规则（幂等，不会重复跑）：
  - 报告已存在（jiaopei/reports/<日期>/<课程>_report.json）→ 跳过
  - 同目录存在 pipeline_failed.json → 跳过（人工排查后删除该文件可触发重跑）
  - meta.json 上传未满 90 秒（分段可能还在传）→ 下一轮再处理

启动：注册表 Run 键 ClassroomAutoPipeline（开机自启，无窗口），或手动：
  C:\\recorder\\venv\\Scripts\\pythonw.exe C:\\recorder\\cloud\\auto_pipeline.py

日志：C:\\recorder\\logs\\auto_pipeline.log（5×5MB 滚动）
心跳：C:\\recorder\\auto_pipeline_heartbeat.txt（每轮更新时间戳）
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from cos_client import CosStore
from pipeline import Pipeline
from schedule_feishu import configure as feishu_configure, sync_lessons_file

CONFIG_PATH = Path(r"C:\recorder\cloud\cloud_config.json")
STATE_DIR = Path(r"C:\recorder\cloud_work")
HEARTBEAT = Path(r"C:\recorder\auto_pipeline_heartbeat.txt")
LOG_DIR = Path(r"C:\recorder\logs")
LESSONS_FILE = Path(r"C:\recorder\recorder\lessons.json")  # 飞书课表同步目标
STATUS_FILE = Path(r"C:/recorder/status/pipeline_status.json")  # 供控制台「云端」页读取

POLL_INTERVAL_SEC = 120      # 轮询间隔
META_MIN_AGE_SEC = 90        # meta 到达后等待分段传完的宽限
SCAN_PREFIX = "jiaopei/"     # 扫描前缀（桶专用于本系统）
CLOUD_PROBE_EVERY = 10       # 每 N 轮做一次云端「读可达性」探测（120s×10 = 20 分钟）


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    fh = RotatingFileHandler(LOG_DIR / "auto_pipeline.log", maxBytes=5 * 1024 * 1024,
                             backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logging.getLogger("auto_pipeline")


def _iso_ts(s: str) -> float:
    """COS LastModified（ISO8601 UTC，如 2026-09-14T05:48:12.000Z）→ epoch 秒。

    解析失败返回 0.0（视为"极旧"），避免因格式异常而误判成"已完成"。
    """
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


_OUT_SUFFIXES = ("_report.json", "_meeting.json", "_transcript.json")


def find_pending(store: CosStore, report_prefix: str, log: logging.Logger,
                 meta_cache: dict | None = None,
                 legacy_bases: dict | None = None) -> list[str]:
    """找出需要处理的 meta.json key 列表（两级判定 + 溯源指纹）。

    完成判定 = 「成品已经包含当前所有 meta 的指纹」，而不是「成品存在」。

    为什么不能只看存在性：一门课可能被录成多段（中途重启 / 换麦 / 断线续录），
    每段的 meta 是同一天的不同文件（tag 带开始时间，故 key 不同），
    但 course_id 相同 → 指向同一个成品 base。若第一段因没识别出教师被归档、
    第二段才是完整的那段，只看存在性会让第二段被永久跳过 —— 那节课
    永远出不了报告。

    为什么不能只看 COS LastModified：历史课程的 meta 时间戳普遍**晚于**成品
    （补传 / 重写 / 归档转换都会刷新存储时间），纯时间比较会一次性把
    几十门历史课全部重跑，白烧 ASR + 声纹费用。故：

      一级（零开销）：成品比全部 meta 都新 → 已处理完，直接跳过。
      二级（仅当 meta 比成品新）：读成品里的 source_metas 指纹比对 ——
        成品是本次改造后产出的才有该字段；旧成品无溯源信息 → 视为已完成。

    三个分支各自只写自己的成品，所以完成判定必须三个都认：
      上课 → <course_id>_report.json
      会议 → <course_id>_meeting.json
      仅存档 → <course_id>_transcript.json
    只认 report.json 会让"会议 / 仅存档"的课程永远判不了完成，
    被每个轮询周期反复重跑（重复 ASR + 声纹，持续计费）。
    """
    try:
        entries = store.list_entries(SCAN_PREFIX)   # {key: LastModified}
    except Exception as exc:  # noqa: BLE001
        log.error("列出 COS 对象失败：%s", exc)
        return []

    failed_keys = {k for k in entries if k.endswith("_pipeline_failed.json")}

    # base → 该课程最新的一份成品 key
    prod_key_of: dict[str, str] = {}
    for k, last_mod in entries.items():
        for suf in _OUT_SUFFIXES:
            if k.endswith(suf):
                base = k[:-len(suf)]        # → {prefix}/{date}/{course_id}
                cur = prod_key_of.get(base)
                if cur is None or _iso_ts(last_mod) > _iso_ts(entries.get(cur, "")):
                    prod_key_of[base] = k
                break

    def _load(mk: str) -> dict | None:
        """读 meta，带 LastModified 缓存（每轮只重读变化过的 meta）。"""
        lm = entries.get(mk, "")
        if meta_cache is not None:
            hit = meta_cache.get(mk)
            if hit and hit[0] == lm:
                return hit[1]
        try:
            m = store.get_json(mk)
        except Exception as exc:  # noqa: BLE001
            log.warning("读取 meta 失败 %s：%s", mk, exc)
            return None
        if meta_cache is not None:
            meta_cache[mk] = (lm, m)
        return m

    # meta 按成品 base 归组（一门课可能对应多个 meta：分段录制 / 补录）
    by_base: dict[str, list[str]] = {}
    for mk in entries:
        if not mk.endswith("_meta.json"):
            continue
        if mk.replace("_meta.json", "_pipeline_failed.json") in failed_keys:
            continue  # 已失败过，等人工处理（删除该标记即可重跑）
        meta = _load(mk)
        if meta is None:
            continue
        date_str = (meta.get("start") or "")[:10].replace("-", "")
        base = f"{report_prefix}/{date_str}/{meta.get('course_id', 'unknown')}"
        by_base.setdefault(base, []).append(mk)

    pending: list[str] = []
    for base, mks in sorted(by_base.items()):
        prod_key = prod_key_of.get(base)
        if prod_key is None:
            pending.extend(mks)                 # 从未产出 → 要处理
            continue

        # ---- 一级：成品比全部 meta 都新 → 已处理完（绝大多数课程走这条）----
        p_ts = _iso_ts(entries.get(prod_key, ""))
        if all(_iso_ts(entries.get(mk, "")) <= p_ts for mk in mks):
            continue

        # ---- 二级：meta 比成品新，读成品溯源确认是否真有新录音 ----
        # 旧成品（改造前产出，无 source_metas）会被反复命中二级分支；缓存其
        # meta 集合，避免每轮重复拉取体积较大的成品 JSON（下行流量按 GB 计费）。
        fingerprints = tuple(sorted(mks))
        if legacy_bases is not None and legacy_bases.get(base) == fingerprints:
            continue
        try:
            prod = store.get_json(prod_key)
        except Exception as exc:  # noqa: BLE001
            log.warning("读取成品失败 %s：%s", prod_key, exc)
            continue
        src = prod.get("source_metas")
        if not src:
            if legacy_bases is not None:
                legacy_bases[base] = fingerprints
            if len(mks) > 1:
                # 旧成品 + 同课程多段录音 → 无法确定它覆盖了哪几段，一律重跑补齐
                log.info("旧成品未覆盖全部 %d 段录音，重新处理：%s", len(mks), base)
                pending.extend(mks)
            else:
                # 单段：时间戳差异只是存储层噪声（补传/重写/归档转换），不得据此重跑
                log.info("成品无溯源信息，按已完成处理：%s", base)
            continue
        current = {mk: (_load(mk) or {}).get("uploaded_at", "") for mk in mks}
        if all(src.get(mk) == v for mk, v in current.items()):
            continue                            # 指纹一致 → 无新数据
        new_metas = [mk for mk in mks if mk not in src]
        log.info("检测到课程有新录音（%s），重新处理：%s",
                 f"新增 {len(new_metas)} 段" if new_metas else "meta 已更新", base)
        pending.extend(mks)
    return pending


def probe_cloud_readable(store: CosStore, log: logging.Logger) -> dict:
    """探测云端「读」是否真的可用，结果写进状态文件供控制台显示。

    为什么要单独探测（2026-09-15 教训）：
      ListObjects 在账户欠费 / 权限受限时**仍然返回成功**，而真正干活
      （下载音频、读 meta）走的是 GetObject。只看轮询主流程有没有报错，
      会出现「待处理 0 门 → 界面一切正常」的假象 —— 实际云端早已停摆
      （9/14 13:55 起 COS 返回 UnavailableForLegalReasons / account is arrears，
       而流水线在界面上完全看不出来，停了整整一天没人知道）。

    做法：挑最近上传的一个 `_meta.json`（几 KB）真读一次。每 20 分钟一次，
    请求量可忽略。
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        entries = store.list_entries(SCAN_PREFIX)
    except Exception as exc:  # noqa: BLE001
        log.warning("云端探测：列表失败 %s", exc)
        return {"ok": False, "at": stamp, "stage": "list",
                "error": f"{type(exc).__name__}: {exc}"[:200]}
    metas = [k for k in entries if k.endswith("_meta.json")]
    if not metas:
        return {"ok": True, "at": stamp, "note": "桶内暂无 meta 对象"}
    # LastModified 是 ISO8601 字符串，字典序即时间序
    key = max(metas, key=lambda k: entries[k])
    try:
        store.get_json(key)
    except Exception as exc:  # noqa: BLE001
        log.warning("云端探测：读取失败 %s", exc)
        return {"ok": False, "at": stamp, "stage": "get",
                "probe_key": key, "error": f"{type(exc).__name__}: {exc}"[:200]}
    return {"ok": True, "at": stamp, "probe_key": key}


def _acquire_singleton_lock(name: str):
    """单实例守护：锁定 status/<name>.lock 首字节，进程退出由 OS 自动释放。
    已有实例持锁时返回 None；成功时返回文件句柄（须存活至进程结束）。"""
    import msvcrt
    import os
    lock_path = Path(r"C:/recorder/status") / f"{name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    fh.seek(0)
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return None
    fh.seek(1)
    fh.write(f" pid={os.getpid()}".encode())
    fh.flush()
    return fh


def main() -> int:
    log = setup_logging()
    _singleton = _acquire_singleton_lock("auto_pipeline")
    if _singleton is None:
        log.warning("已有 auto_pipeline 实例在运行，本次启动退出")
        return 0
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    feishu_configure(config.get("feishu", {}) or {})
    report_prefix = config.get("report", {}).get("result_prefix", "jiaopei/reports")
    cos_cfg = config["cos"]
    store = CosStore(bucket=cos_cfg["bucket"], region=cos_cfg["region"],
                     secret_id=cos_cfg["secret_id"], secret_key=cos_cfg["secret_key"])
    log.info("自动教评流水线已启动：轮询 %s，间隔 %ds", SCAN_PREFIX, POLL_INTERVAL_SEC)

    # 对外可见的运行时状态（控制台「云端」页轮询它，1 秒读一次，零成本）
    status: dict = {
        "pid": os.getpid(),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "interval_sec": POLL_INTERVAL_SEC,
        "meta_min_age_sec": META_MIN_AGE_SEC,
        "scan_prefix": SCAN_PREFIX,
        "rounds": 0,
        "processed_total": 0,
        "pending_count": 0,
        "waiting": [],
        "current": None,
        "last_result": None,
        "recent": [],
        "cloud_health": None,   # 云端读可达性探测结果（见 probe_cloud_readable）
    }

    def flush(**kw) -> None:
        status.update(kw)
        status["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            tmp = STATUS_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(STATUS_FILE)
        except Exception:  # noqa: BLE001
            pass  # 状态文件写不出去不该影响流水线本身

    flush()

    # 启动即同步一次飞书课表 → lessons.json（recorder 逐秒读取，按真实课表自动录制）
    try:
        sync_lessons_file(LESSONS_FILE, ctx={"config": config}, force=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("启动时课表同步失败（不影响流水线）：%s", exc)

    # 记录首次见到的 key 及时间，用于"到达 90 秒宽限"
    first_seen: dict[str, float] = {}
    # meta 内容缓存：{key: (LastModified, meta)}，只有时间戳变化才重新拉取
    meta_cache: dict[str, tuple[str, dict]] = {}
    # 已确认「无溯源信息的旧成品」：{base: meta key 元组}，避免每轮重复拉大 JSON
    legacy_bases: dict[str, tuple] = {}

    while True:
        try:
            HEARTBEAT.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            status["rounds"] += 1
            # 低频探测云端「读」可达性（欠费/权限故障只有 GetObject 才暴露，
            # ListObjects 欠费时照样成功 —— 见 probe_cloud_readable 注释）
            if status["rounds"] % CLOUD_PROBE_EVERY == 1:
                flush(cloud_health=probe_cloud_readable(store, log))
            # 课表同步（内部限频 10 分钟；失败保留旧文件）
            try:
                sync_lessons_file(LESSONS_FILE, ctx={"config": config})
                status["schedule_sync"] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                           "ok": True}
            except Exception as exc:  # noqa: BLE001
                log.warning("课表同步异常（忽略）：%s", exc)
                status["schedule_sync"] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                           "ok": False, "error": str(exc)[:200]}
            pending = find_pending(store, report_prefix, log,
                                   meta_cache, legacy_bases)
            now = time.time()
            waiting: list[dict] = []
            for mk in pending:
                seen = first_seen.setdefault(mk, now)
                if now - seen < META_MIN_AGE_SEC:
                    log.info("新课程等待宽限：%s（%.0fs/%ds）", mk, now - seen,
                             META_MIN_AGE_SEC)
                    waiting.append({"key": mk, "waited_sec": int(now - seen)})
                    continue
                log.info("发现待处理课程：%s，启动流水线", mk)
                flush(pending_count=len(pending), waiting=waiting,
                      last_scan_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                      current={"meta_key": mk,
                               "started_at": time.strftime("%Y-%m-%d %H:%M:%S")})
                started = time.time()
                try:
                    result = Pipeline(config).process(
                        bucket=cos_cfg["bucket"], meta_key=mk)
                    log.info("课程处理完成：%s → %s", mk, result.get("status"))
                    status["processed_total"] += 1
                    status["last_result"] = {
                        "meta_key": mk, "ok": True,
                        "status": str(result.get("status")),
                        "elapsed_sec": round(time.time() - started, 1),
                        "at": time.strftime("%Y-%m-%d %H:%M:%S")}
                except Exception as exc:  # noqa: BLE001
                    log.error("课程处理异常：%s：%s", mk, exc)
                    status["last_result"] = {
                        "meta_key": mk, "ok": False,
                        "status": f"{type(exc).__name__}: {exc}"[:200],
                        "elapsed_sec": round(time.time() - started, 1),
                        "at": time.strftime("%Y-%m-%d %H:%M:%S")}
                status["recent"] = ([status["last_result"]]
                                    + (status.get("recent") or []))[:10]
                flush(current=None)
                first_seen.pop(mk, None)
            flush(pending_count=len(pending), waiting=waiting,
                  last_scan_at=time.strftime("%Y-%m-%d %H:%M:%S"), current=None)
        except Exception as exc:  # noqa: BLE001
            log.error("轮询循环异常（继续运行）：%s", exc)
            flush(last_error=f"{type(exc).__name__}: {exc}"[:200])
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main())
