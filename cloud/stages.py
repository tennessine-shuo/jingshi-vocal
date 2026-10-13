"""流水线各 AI 环节实现（方案B · 云端 · 正式版）。

统一约定：
- ctx: dict，流水线上下文，由 pipeline 注入：
    ctx["store"]    CosStore 实例
    ctx["config"]   cloud_config dict（asr / voiceprint / workdir 等）
    ctx["workdir"]  本地工作目录（已创建）
    ctx["_asr"]     ASR 任务结果缓存（diarize / asr_transcribe 共用一次识别）
- 各环节输入/输出均为 dict/list，字段见各函数 docstring。

云 API（腾讯云）：
- 录音文件识别 CreateRecTask / DescribeTaskStatus（asr v20190614）
  SpeakerDiarization=1 开启说话人分离（16k_zh + ChannelNum=1 可用）
- 说话人验证 1:N  VoicePrintGroupVerify（asr v20190614，ASR+ 声纹）
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from tencentcloud.asr.v20190614 import asr_client, models as asr_models
from tencentcloud.common import credential
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile

log = logging.getLogger("stages")

# ---------------------------------------------------------------- 公共工具


def _find_ffmpeg() -> str:
    """本地：捆绑副本 → PATH → 装机默认位置；SCF：需挂载 ffmpeg 层并保证在 PATH。"""
    bundled = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "ffmpeg", "bin", "ffmpeg.exe")
    if os.path.exists(bundled):
        return bundled
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    fallback = r"C:\ffmpeg\bin\ffmpeg.exe"
    if os.path.exists(fallback):
        return fallback
    raise RuntimeError("找不到 ffmpeg（请加入 PATH 或安装到 C:\\ffmpeg）")


# Windows 下隐藏 ffmpeg 等子进程的控制台窗口（否则 pythonw 后台运行时频繁弹黑窗）
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    log.debug("执行: %s", " ".join(cmd[:3]) + " ...")
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600,
                          creationflags=_NO_WINDOW)


def _asr_client(ctx: dict) -> asr_client.AsrClient:
    cos_cfg = ctx["config"]["cos"]
    cred = credential.Credential(cos_cfg["secret_id"], cos_cfg["secret_key"])
    hp = HttpProfile(); hp.endpoint = "asr.tencentcloudapi.com"
    cp = ClientProfile(); cp.httpProfile = hp
    return asr_client.AsrClient(cred, cos_cfg.get("region", "ap-shanghai"), cp)


# ---------------------------------------------------------------- 1. 转码 + VAD


def transcode_and_vad(meta: dict, ctx: dict) -> list[dict]:
    """下载分段 → 合并转码 16kHz 单声道 → 静音检测。

    返回: [{start_sec, end_sec}]（有效语音段时间轴）
    副作用: ctx["merged_wav"]（本地 16k wav）、ctx["merged_key"]、ctx["merged_url"]
    """
    store = ctx["store"]
    workdir = Path(ctx["workdir"])
    course_id = meta.get("course_id", "unknown")
    ffmpeg = _find_ffmpeg()

    # 1) 下载全部分段（校验 sha256）
    local_parts = []
    for i, seg in enumerate(meta["segments"]):
        dst = str(workdir / f"seg_{i:05d}.wav")
        store.download(seg["key"], dst, expect_sha256=seg.get("sha256"))
        local_parts.append(dst)
    log.info("已下载 %d 个分段", len(local_parts))

    # 2) 逐段转 16k 单声道（容错：末段 WAV 头可能未封尾，ffmpeg 可正常读数据区）
    pcm_parts = []
    for i, src in enumerate(local_parts):
        dst = str(workdir / f"pcm_{i:05d}.wav")
        r = _run([ffmpeg, "-y", "-loglevel", "error", "-i", src,
                  "-ar", "16000", "-ac", "1", "-acodec", "pcm_s16le", dst])
        if r.returncode != 0:
            log.warning("分段 %d 转码失败，跳过: %s", i, r.stderr.strip()[:200])
            continue
        pcm_parts.append(dst)
    if not pcm_parts:
        raise RuntimeError("全部音频分段转码失败")

    # 3) 合并（PCM wav 可无损 concat）
    concat_list = workdir / "concat.txt"
    concat_list.write_text(
        "\n".join(f"file '{p.replace(chr(92), '/')}'" for p in pcm_parts), encoding="utf-8")
    merged = str(workdir / "merged_16k.wav")
    r = _run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
              "-i", str(concat_list), "-c", "copy", merged])
    if r.returncode != 0:
        raise RuntimeError(f"合并失败: {r.stderr.strip()[:300]}")
    ctx["merged_wav"] = merged

    # 4) 上传合并件 → 生成预签名 URL（供 ASR 拉取）
    date_str = (meta.get("start") or "")[:10].replace("-", "")
    merged_key = f"jiaopei/_pipeline/{meta.get('room_id', 'room')}/{date_str}/{course_id}_merged_16k.wav"
    store.put_file(merged_key, merged)
    ctx["merged_key"] = merged_key
    ctx["merged_url"] = store.presigned_url(merged_key, expired=6 * 3600)
    log.info("合并件已上传 %s", merged_key)

    # 5) 静音检测 → 有效语音段时间轴
    r = _run([ffmpeg, "-i", merged, "-af", "silencedetect=noise=-35dB:d=0.8",
              "-f", "null", "-"])
    return _parse_silence(r.stderr, ctx)


def _parse_silence(ffmpeg_stderr: str, ctx: dict) -> list[dict]:
    """解析 silencedetect 输出，得到语音段（静音段的补集）。"""
    starts = [float(m) for m in re.findall(r"silence_start: ([\d.]+)", ffmpeg_stderr)]
    ends = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", ffmpeg_stderr)]
    duration = 0.0
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", ffmpeg_stderr)
    if m:
        duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    ctx["duration_sec"] = duration

    silences, cur = [], 0.0
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else duration
        silences.append((s, e)); cur = e
    # 语音 = 静音补集
    speech, prev_end = [], 0.0
    for s, e in silences:
        if s - prev_end > 0.3:
            speech.append({"start_sec": round(prev_end, 2), "end_sec": round(s, 2)})
        prev_end = e
    if duration - prev_end > 0.3:
        speech.append({"start_sec": round(prev_end, 2), "end_sec": round(duration, 2)})
    log.info("静音检测: 总时长 %.0fs，有效语音段 %d 个", duration, len(speech))
    return speech


# ---------------------------------------------------------------- 2. ASR（含角色分离，一次任务两用）


def _run_asr_task(meta: dict, ctx: dict) -> list[dict]:
    """提交录音文件识别（开启说话人分离）并轮询，返回 ResultDetail 句级列表。

    结果缓存于 ctx["_asr"]，diarize / asr_transcribe 共用，避免重复计费。
    """
    if "_asr" in ctx:
        return ctx["_asr"]

    asr_cfg = ctx["config"].get("asr", {})
    client = _asr_client(ctx)

    req = asr_models.CreateRecTaskRequest()
    req.EngineModelType = asr_cfg.get("engine", "16k_zh")
    req.ChannelNum = 1
    req.ResTextFormat = 0                     # 句级时间戳 + 文本
    req.SourceType = 0                        # URL 拉取
    req.Url = ctx["merged_url"]
    req.SpeakerDiarization = 1                # 开启说话人分离
    req.SpeakerNumber = asr_cfg.get("speaker_number", 2)  # 1对1 → 2 人
    req.ConvertNumMode = 3                    # 数学场景数字智能转换
    req.ResTextFormat = 2                     # 识别结果返回 JSON（含逐句时间戳与说话人）
    resp = client.CreateRecTask(req)
    task_id = resp.Data.TaskId
    log.info("ASR 任务已提交 TaskId=%s", task_id)

    interval = asr_cfg.get("poll_interval_sec", 10)
    timeout = asr_cfg.get("poll_timeout_sec", 1800)
    deadline = time.monotonic() + timeout
    while True:
        q = asr_models.DescribeTaskStatusRequest()
        q.TaskId = task_id
        st = client.DescribeTaskStatus(q)
        status = st.Data.Status              # 0等待 1执行中 2成功 3失败
        if status == 2:
            break
        if status == 3:
            raise RuntimeError(f"ASR 任务失败: {st.Data.ErrorMsg}")
        if time.monotonic() > deadline:
            raise RuntimeError("ASR 任务轮询超时")
        time.sleep(interval)

    raw = st.Data.ResultStr if hasattr(st.Data, "ResultStr") and st.Data.ResultStr else st.Data.Result
    try:
        result = json.loads(raw)
        detail = result.get("Response", result).get("ResultDetail", []) or []
    except (json.JSONDecodeError, AttributeError):
        # 兜底：ResTextFormat 未生效时返回的是文本格式，每行形如
        # [0:0.000,0:1.310,0]  内容  → [声道:开始,声道:结束,说话人ID]
        detail = []
        for line in raw.splitlines():
            m = re.match(r"\[(\d+):(\d+(?:\.\d+)?),(\d+):(\d+(?:\.\d+)?),(\d+)\]\s*(.*)", line.strip())
            if m and m.group(6).strip():
                start_sec = int(m.group(1)) * 60 + float(m.group(2))
                end_sec = int(m.group(3)) * 60 + float(m.group(4))
                detail.append({"StartMs": start_sec * 1000,
                               "EndMs": end_sec * 1000,
                               "SpeakerId": int(m.group(5)),
                               "FinalSentence": m.group(6).strip()})
        log.info("ASR 结果为文本格式，按行解析出 %d 句", len(detail))
    sentences = []
    for d in detail:
        text = d.get("FinalSentence") or d.get("SliceSentence") or ""
        if not text.strip():
            continue
        sentences.append({
            "start_sec": round(d["StartMs"] / 1000, 2),
            "end_sec": round(d["EndMs"] / 1000, 2),
            "text": text.strip(),
            "speaker_id": d.get("SpeakerId", 0),
        })
    log.info("ASR 完成: %d 句，说话人 %s", len(sentences),
             sorted({s["speaker_id"] for s in sentences}))
    ctx["_asr"] = sentences
    return sentences


def diarize(meta: dict, ctx: dict) -> list[dict]:
    """说话人分离时间轴（来自 ASR 角色分离结果，合并同人相邻句）。

    返回: [{speaker_id, start_sec, end_sec}]
    """
    sentences = _run_asr_task(meta, ctx)
    timeline: list[dict] = []
    for s in sentences:
        if timeline and timeline[-1]["speaker_id"] == s["speaker_id"] and \
                s["start_sec"] - timeline[-1]["end_sec"] < 1.0:
            timeline[-1]["end_sec"] = s["end_sec"]
        else:
            timeline.append({"speaker_id": s["speaker_id"],
                             "start_sec": s["start_sec"], "end_sec": s["end_sec"]})
    return timeline


def asr_transcribe(vad_segments: list[dict], meta: dict, ctx: dict) -> list[dict]:
    """ASR 转写（句级时间戳 + 文本 + speaker_id）。

    返回: [{start_sec, end_sec, text, speaker_id}]
    """
    return _run_asr_task(meta, ctx)


# ---------------------------------------------------------------- 3. 声纹 1:N


def voiceprint_match(speakers: list[dict], meta: dict, ctx: dict) -> list[dict]:
    """声纹 1:N 比对：speaker → 人员身份。

    返回: [{speaker_id, person_id, person_name, role, confidence, method}]
    引擎由 config.voiceprint.engine 选择：
      cloud（默认）→ 腾讯云 VoicePrintGroupVerify
      local        → 本地 CAM++ ONNX（cloud/voiceprint_local.py，零调用成本）
    任一引擎失败都逐级降级，最终兜底启发式（发言时长最长者 = meta 中的老师）。
    """
    vp_cfg = ctx["config"].get("voiceprint", {})
    sentences = ctx.get("_asr") or _run_asr_task(meta, ctx)

    if (vp_cfg.get("engine") or "cloud").strip().lower() == "local":
        try:
            return _voiceprint_local_match(speakers, sentences, vp_cfg, meta, ctx)
        except Exception as e:  # noqa: BLE001
            log.warning("本地声纹比对失败，回退云端引擎: %s", e)

    registry = _load_registry(vp_cfg, ctx)
    if vp_cfg.get("enable_api", True) and registry:
        try:
            return _voiceprint_api_match(speakers, sentences, registry, vp_cfg, meta, ctx)
        except Exception as e:  # noqa: BLE001
            log.warning("声纹 API 比对失败，降级为启发式: %s", e)
    else:
        log.info("声纹注册库为空或已禁用，使用启发式角色分配")
    return _heuristic_match(speakers, sentences, meta)


def _load_registry(vp_cfg: dict, ctx: dict) -> dict:
    """声纹注册库：voiceprint_id -> {person_id, person_name, role}。"""
    path = vp_cfg.get("registry_file", "")
    if not path:
        return {}
    p = Path(path)
    if not p.is_absolute():
        p = Path(ctx["workdir"]).parent / path
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _voiceprint_local_match(speakers, sentences, vp_cfg, meta, ctx) -> list[dict]:
    """本地声纹 1:N 比对（CAM++ ONNX，零 API 调用成本）。

    与云端版的差异：
      - 相似度是**余弦**（0~1 量级），不是腾讯云的 0~100 分，阈值需单独标定：
        config.voiceprint.local_threshold / local_floor / local_margin
      - 判定沿用同一套复合判据：top1 ≥ threshold，或 top1 ≥ floor 且领先 ≥ margin

    ⚠️ 注册库必须与使用场景**同域**：拿近场录音注册、再对远场课堂片段比对，
       余弦会从 0.8 量级掉到 0.3 量级，区分度崩塌。建议用课堂片段注册。
    """
    from voiceprint_local import LocalVoiceprint   # 延迟导入：非 local 模式不依赖

    ffmpeg = _find_ffmpeg()
    merged = ctx["merged_wav"]
    db = LocalVoiceprint(
        model_path=vp_cfg.get("local_model",
                              r"C:\recorder\models\campplus_sv_zh_16k.onnx"),
        db_path=vp_cfg.get("local_db",
                           r"C:\recorder\cloud\voiceprint_local_db.json"))
    threshold = float(vp_cfg.get("local_threshold", 0.62))
    floor = float(vp_cfg.get("local_floor", 0.30))
    margin = float(vp_cfg.get("local_margin", 0.12))

    uniq: dict = {}
    for sp in speakers:
        uniq.setdefault(sp["speaker_id"], sp)
    targets = list(uniq.values())
    log.info("本地声纹比对：%d 个音色 / 库内 %d 人（阈值 %.2f｜底线 %.2f｜领先 %.2f）",
             len(targets), len(db.persons), threshold, floor, margin)

    n_vote = max(1, int(vp_cfg.get("vote_clips", 3)))
    results = []
    for sp in targets:
        clips = _extract_speaker_clips(ffmpeg, merged, sp["speaker_id"],
                                       sentences, ctx["workdir"], n_clips=n_vote)
        if not clips:
            results.append({**sp, "person_id": "unregistered",
                            "person_name": "未识别学生", "role": "student",
                            "confidence": 0.0, "method": "no_audio"})
            continue
        # 多片段投票：verify_multi_sources 内部按「票数 → 中位余弦」两级聚合 ——
        # 票数决定"谁是多数片段的第一名"，中位余弦抗单点极值。
        res = db.verify_multi_sources(clips, topk=2)
        if not res.get("hit"):
            results.append({**sp, "person_id": "unregistered",
                            "person_name": "未识别学生", "role": "student",
                            "confidence": 0.0,
                            "method": f"local_{res.get('reason', 'miss')}"})
            continue
        t1 = res["tops"][0]
        c1 = t1["median"]
        c2 = res["tops"][1]["median"] if len(res["tops"]) > 1 else 0.0
        log.info("本地声纹投票 speaker=%s：%d 片段 → %s", sp["speaker_id"],
                 res["clips"], " | ".join(
                     f"{t['person_name']} 票{t['votes']} 中位{t['median']:.3f}"
                     for t in res["tops"][:3]))
        hit = c1 >= threshold or (c1 >= floor and (c1 - c2) >= margin)
        if hit:
            results.append({**sp, "person_id": t1["person_id"],
                            "person_name": t1["person_name"],
                            "role": t1["role"],
                            "confidence": round(c1, 2),
                            "method": "voiceprint_local",
                            "match_score": round(c1 * 100, 1),
                            "match_margin": round((c1 - c2) * 100, 1),
                            "match_votes": f"{t1['votes']}/{res['clips']}"})
        else:
            results.append({**sp, "person_id": "unregistered",
                            "person_name": "未识别学生", "role": "student",
                            "confidence": round(c1, 2),
                            "method": "voiceprint_local_miss",
                            "match_score": round(c1 * 100, 1),
                            "match_votes": f"{t1['votes']}/{res['clips']}"})
    return results


def _voiceprint_api_match(speakers, sentences, registry, vp_cfg, meta, ctx) -> list[dict]:
    client = _asr_client(ctx)
    ffmpeg = _find_ffmpeg()
    merged = ctx["merged_wav"]
    threshold = vp_cfg.get("match_threshold", 70)
    # 复合判据：教室录音（远场/混响/降噪）会让绝对分整体衰减，单独用绝对阈值
    # 会把"正确但偏低"的教师判定成未识别。故额外要求 top-1 明显领先第二名。
    #   accept = score >= floor  AND  (score - runner_up) >= margin
    floor = vp_cfg.get("match_floor", min(threshold, 60))
    margin = vp_cfg.get("match_margin", 10)
    group_id = vp_cfg.get("group_id", "jiaopei_voiceprints")

    # diarize() 返回的是「逐轮发言时间轴」——同一个 speaker_id 会出现成百上千次
    # （一节 2h 课 ASR 约 1600 句 → 时间轴约 1400 条）。声纹只需每个**音色**比对
    # 一次，若直接遍历时间轴就会打出上千次 VoicePrintGroupVerify，
    # 单价 0.0042 元/次 → 一节课声纹费从 0.008 元暴涨到约 6 元（×700）。
    # 去重对下游完全等价：clean_segments 用 speaker_id 建字典，重复项本就互相覆盖。
    uniq: dict = {}
    for sp in speakers:
        uniq.setdefault(sp["speaker_id"], sp)
    targets = list(uniq.values())
    if len(targets) < len(speakers):
        log.info("声纹比对：逐轮时间轴 %d 条 → 去重后 %d 个音色，"
                 "省下 %d 次 API 调用", len(speakers), len(targets),
                 len(speakers) - len(targets))

    # 多片段投票：每个音色取 n_vote 条**独立**片段分别比对，再按票数聚合。
    # 调用量 = 音色数 × n_vote（一节课 2 音色 × 3 = 6 次 ≈ 0.025 元），
    # 远比"拼接单片段"稳健，成本可忽略。
    n_vote = max(1, int(vp_cfg.get("vote_clips", 3)))
    results = []
    for sp in targets:
        clips = _extract_speaker_clips(ffmpeg, merged, sp["speaker_id"],
                                       sentences, ctx["workdir"], n_clips=n_vote)
        if not clips:
            results.append({**sp, "person_id": "unregistered",
                            "person_name": "未识别学生", "role": "student",
                            "confidence": 0.0, "method": "no_audio"})
            continue

        votes: dict[str, int] = {}
        pool: dict[str, list[float]] = {}
        for clip in clips:
            data_b64 = base64.b64encode(Path(clip).read_bytes()).decode()
            req = asr_models.VoicePrintGroupVerifyRequest()
            req.VoiceFormat = 1                # wav
            req.SampleRate = 16000
            req.Data = data_b64
            req.GroupId = group_id
            req.TopN = 2                       # 需要第二名算领先幅度
            try:
                resp = client.VoicePrintGroupVerify(req)
            except Exception as exc:  # noqa: BLE001  单片失败不拖累其余投票
                log.warning("声纹片段比对失败（%s）：%s", Path(clip).name, exc)
                continue
            top, runner = _parse_verify_top2(resp)
            if not top:
                continue
            votes[top["voiceprint_id"]] = votes.get(top["voiceprint_id"], 0) + 1
            for cand in (top, runner):          # 前二名都计入分数池，供算领先幅度
                if cand:
                    pool.setdefault(cand["voiceprint_id"], []).append(cand["score"])

        if not pool:
            results.append({**sp, "person_id": "unregistered",
                            "person_name": "未识别学生", "role": "student",
                            "confidence": 0.0, "method": "voiceprint_miss",
                            "match_score": 0.0})
            continue

        # 聚合：票数优先（谁是多数片段的第一名），其次中位分（抗单点极值）
        agg = [{"voiceprint_id": vid,
                "votes": votes.get(vid, 0),
                "median": float(sorted(sc)[len(sc) // 2]),
                "max": max(sc), "n": len(sc)}
               for vid, sc in pool.items()]
        agg.sort(key=lambda x: (x["votes"], x["median"]), reverse=True)
        t1 = agg[0]
        t2 = agg[1] if len(agg) > 1 else None
        lead = t1["median"] - (t2["median"] if t2 else 0.0)
        log.info("声纹投票 speaker=%s：%d 片段 → %s", sp["speaker_id"], len(clips),
                 " | ".join(
                     f"{registry.get(x['voiceprint_id'], {}).get('person_name', '?')}"
                     f" 票{x['votes']} 中位{x['median']:.1f}" for x in agg[:3]))

        hit = (t1["voiceprint_id"] in registry
               and (t1["median"] >= threshold
                    or (t1["median"] >= floor and lead >= margin)))
        if hit:
            person = registry[t1["voiceprint_id"]]
            results.append({**sp, "person_id": person["person_id"],
                            "person_name": person["person_name"],
                            "role": person["role"],
                            "confidence": round(t1["median"] / 100, 2),
                            "method": "voiceprint",
                            "match_score": round(t1["median"], 1),
                            "match_margin": round(lead, 1),
                            "match_votes": f"{t1['votes']}/{len(clips)}"})
        else:
            results.append({**sp, "person_id": "unregistered",
                            "person_name": "未识别学生", "role": "student",
                            "confidence": round(t1["median"] / 100, 2),
                            "method": "voiceprint_miss",
                            "match_score": round(t1["median"], 1),
                            "match_votes": f"{t1['votes']}/{len(clips)}"})
    return results


def _parse_verify_top(resp) -> dict | None:
    """解析 VoicePrintGroupVerify 响应（Data.VerifyTops[0]: VoicePrintId/Score）。"""
    items = resp.Data.VerifyTops if resp.Data else None
    if not items:
        return None
    first = items[0]
    return {"voiceprint_id": first.VoicePrintId, "score": float(first.Score)}


def _parse_verify_top2(resp) -> tuple[dict | None, dict | None]:
    """解析前两名（Top1, Top2）。

    Top2 用于计算领先幅度——教室录音绝对分普遍偏低时，
    "正确但偏低"与"错误但偏高"难以靠单一阈值区分，靠领先幅度更稳。
    """
    items = resp.Data.VerifyTops if resp.Data else None
    if not items:
        return None, None
    t1 = {"voiceprint_id": items[0].VoicePrintId, "score": float(items[0].Score)}
    t2 = ({"voiceprint_id": items[1].VoicePrintId, "score": float(items[1].Score)}
          if len(items) > 1 else None)
    return t1, t2


def _extract_speaker_clip(ffmpeg, merged, speaker_id, sentences,
                          workdir, max_sec=20) -> str | None:
    """拼接该说话人最长若干句，截取 max_sec 语音。

    硬上限：声纹 API 要求音频 ≤30s / ≤2MB，这里把 max_sec 钳到 25s，
    且逐句裁剪时按剩余额度截断（长句会被切短），避免超限报
    InvalidParameterValue.ErrorVoicedataTooLong。
    """
    max_sec = min(max_sec, 25)
    own = [s for s in sentences if s["speaker_id"] == speaker_id]
    own.sort(key=lambda s: s["end_sec"] - s["start_sec"], reverse=True)
    picked, total = [], 0.0
    for s in own:
        dur = s["end_sec"] - s["start_sec"]
        if dur < 1.0:
            continue
        room = max_sec - total
        if room <= 0:
            break
        if dur > room:                      # 长句截断，不整句吞入
            s = {**s, "end_sec": s["start_sec"] + room}
            dur = room
        picked.append(s); total += dur
        if total >= max_sec:
            break
    if not picked:
        return None
    # 逐句裁剪后拼接
    parts = []
    for i, s in enumerate(sorted(picked, key=lambda x: x["start_sec"])):
        dst = str(Path(workdir) / f"vp_{speaker_id}_{i}.wav")
        r = _run([ffmpeg, "-y", "-loglevel", "error", "-ss", str(s["start_sec"]),
                  "-t", str(s["end_sec"] - s["start_sec"]),
                  "-i", merged, "-acodec", "pcm_s16le", dst])
        if r.returncode == 0:
            parts.append(dst)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    lst = Path(workdir) / f"vp_{speaker_id}_list.txt"
    lst.write_text("\n".join(f"file '{p.replace(chr(92), '/')}'" for p in parts),
                   encoding="utf-8")
    out = str(Path(workdir) / f"vp_{speaker_id}.wav")
    r = _run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
              "-i", str(lst), "-c", "copy", out])
    return out if r.returncode == 0 else None


def _extract_speaker_clips(ffmpeg, merged, speaker_id, sentences, workdir,
                           n_clips: int = 3, max_clip_sec: float = 12.0,
                           min_clip_sec: float = 2.0) -> list[str]:
    """取出该说话人 n_clips 条**互相独立**的语音片段（不做拼接）。

    为什么不再拼接成一条：
      拼接会先把多个片段的声学条件平均掉，且**任意一段被噪声/串音污染，都会
      拖累整条音频**；独立片段各自出一个 embedding、各投一票，坏片段只丢自己
      那一票。标定（tools/calibrate_local_vp.py）显示同课异人的余弦区间与同人
      区间会重叠、最差领先幅度仅 0.025，投票是拉大这一差距的主要手段。

    选片策略：按时间排序后均分成 n_clips 桶，每桶取**最长的一句** ——
      既保证时间上分散（避免全挤在同一段对话里），又保证每片语音充分。
    片段长度钳在 [min_clip_sec, max_clip_sec]：太短 embedding 噪声大
      （标定：40s 切 10s×4 段反而比整段差），太长则超出声纹 API 的 30s 限制。
    """
    own = [s for s in sentences if s["speaker_id"] == speaker_id
           and (s["end_sec"] - s["start_sec"]) >= min_clip_sec]
    if not own:
        return []
    own.sort(key=lambda s: s["start_sec"])
    n = max(1, min(int(n_clips), len(own)))
    picked = []
    for i in range(n):
        lo, hi = i * len(own) // n, (i + 1) * len(own) // n
        bucket = own[lo:hi] or own[lo:lo + 1]
        if bucket:
            picked.append(max(bucket, key=lambda s: s["end_sec"] - s["start_sec"]))

    parts: list[str] = []
    for i, s in enumerate(picked):
        dur = min(s["end_sec"] - s["start_sec"], max_clip_sec)
        dst = str(Path(workdir) / f"vpx_{speaker_id}_{i}.wav")
        r = _run([ffmpeg, "-y", "-loglevel", "error",
                  "-ss", str(s["start_sec"]), "-t", str(dur),
                  "-i", merged, "-acodec", "pcm_s16le", dst])
        if r.returncode == 0 and Path(dst).exists():
            parts.append(dst)
    return parts


def _heuristic_match(speakers, sentences, meta) -> list[dict]:
    """兜底：发言总时长最长者视为老师（1对1场景成立），其余为学生。"""
    totals: dict[int, float] = {}
    for s in sentences:
        totals[s["speaker_id"]] = totals.get(s["speaker_id"], 0) + \
            (s["end_sec"] - s["start_sec"])
    teacher_sp = max(totals, key=totals.get) if totals else None
    teacher_name = meta.get("teacher") or "老师"
    out = []
    for sp in speakers:
        if sp["speaker_id"] == teacher_sp:
            out.append({**sp, "person_id": "heuristic_teacher",
                        "person_name": teacher_name, "role": "teacher",
                        "confidence": 0.6, "method": "heuristic"})
        else:
            out.append({**sp, "person_id": "unregistered",
                        "person_name": "学生", "role": "student",
                        "confidence": 0.0, "method": "heuristic"})
    return out


def heuristic_roles(speakers: list[dict], meta: dict, ctx: dict) -> list[dict]:
    """声纹取消后的角色判定（2026-10-13 起）。

    教师归属采信 meta.teacher（录制端手动选定或课表自带），说话人角色用
    启发式：发言总时长最长者 = 教师，其余 = 学生。零 API 调用、零额外成本。
    """
    sentences = ctx.get("_asr") or _run_asr_task(meta, ctx)
    return _heuristic_match(speakers, sentences, meta)


# ---------------------------------------------------------------- 4. 语义清洗 + 统计

_FILLERS = ("那个那个", "嗯嗯", "呃呃")


def clean_segments(raw: list[dict], speakers: list[dict]) -> list[dict]:
    """语义清洗 + 合并：按 speaker_id 关联身份，去口头禅，合并同人相邻句。

    返回: [{person_id, person_name, role, start_sec, end_sec, text}]
    """
    identity = {sp["speaker_id"]: sp for sp in speakers}
    result = []
    for seg in raw:
        sp = identity.get(seg["speaker_id"], {})
        text = seg["text"]
        for f in _FILLERS:
            text = text.replace(f, "")
        text = text.strip()
        if not text:
            continue
        if result and result[-1]["speaker_id"] == seg["speaker_id"] and \
                seg["start_sec"] - result[-1]["end_sec"] < 3:
            result[-1]["text"] += text
            result[-1]["end_sec"] = seg["end_sec"]
            continue
        result.append({
            "speaker_id": seg["speaker_id"],
            "person_id": sp.get("person_id", "unregistered"),
            "person_name": sp.get("person_name", "未识别学生"),
            "role": sp.get("role", "student"),
            "start_sec": seg["start_sec"],
            "end_sec": seg["end_sec"],
            "text": text,
        })
    return result


def compute_stats(segments: list[dict], meta: dict, ctx: dict | None = None) -> dict:
    """课堂特征统计：发言占比、问答轮次、时长、拖堂、静音占比。"""
    teacher_chars = sum(len(s["text"]) for s in segments if s["role"] == "teacher")
    student_chars = sum(len(s["text"]) for s in segments if s["role"] == "student")
    total = teacher_chars + student_chars
    qa_rounds = sum(
        1 for i in range(1, len(segments))
        if segments[i - 1]["role"] == "teacher" and segments[i]["role"] == "student"
    )

    # 拖堂：实际最后语音时间 - 计划时长
    overrun_sec = 0
    try:
        start = datetime.fromisoformat(meta["start"])
        end = datetime.fromisoformat(meta["end"])
        planned = (end - start).total_seconds()
        if segments:
            actual_last = segments[-1]["end_sec"]
            overrun_sec = max(0, int(actual_last - planned))
    except (KeyError, ValueError):
        planned = 0

    duration = (ctx or {}).get("duration_sec") or meta.get("duration_sec", 0)
    speech_time = sum(s["end_sec"] - s["start_sec"] for s in segments)
    return {
        "duration_sec": int(duration),
        "teacher_chars": teacher_chars,
        "student_chars": student_chars,
        "teacher_ratio": round(teacher_chars / total, 2) if total else 0.0,
        "qa_rounds": qa_rounds,
        "overrun_sec": overrun_sec,
        "silence_ratio": round(1 - speech_time / duration, 2) if duration else 0.0,
    }
