# -*- coding: utf-8 -*-
"""ASR 引擎 A/B 对比：同一段录音分别用「标准 16k_zh」与「大模型2.0 16k_zh_en_2.0」识别。

目的：评估切换到 16k_zh_en_2.0（0.8 元/h）较 16k_zh（1.75 元/h）的转写质量与耗时差异，
供成本优化决策。二者均开启说话人分离（SpeakerDiarization=1，仅 16k 单声道支持）。

用法：python asr_ab_test.py <cos_seg_key> [输出json]
  cos_seg_key  例如 jiaopei/room-01/20260909/潘奕恺-Alevel数学(M1)_20260909_130000_00008.wav
默认把 A/B 结果写到 C:/recorder/reports/asr_ab_result.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from tencentcloud.asr.v20190614 import asr_client, models as asr_models
from tencentcloud.common import credential
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile

from cos_client import CosStore
from stages import _find_ffmpeg

sys.path.insert(0, str(Path(__file__).parent))
CONFIG_PATH = r"C:\recorder\cloud\cloud_config.json"
WORK = r"C:\recorder\cloud_work\_asr_ab"
Path(WORK).mkdir(parents=True, exist_ok=True)

ENGINES = [
    ("标准版 16k_zh", "16k_zh"),
    ("大模型2.0 16k_zh_en_2.0", "16k_zh_en_2.0"),
]


def load_cfg() -> dict:
    return json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))


def make_client(cfg) -> asr_client.AsrClient:
    c = cfg["cos"]
    cred = credential.Credential(c["secret_id"], c["secret_key"])
    hp = HttpProfile(); hp.endpoint = "asr.tencentcloudapi.com"
    cp = ClientProfile(); cp.httpProfile = hp
    return asr_client.AsrClient(cred, c.get("region", "ap-shanghai"), cp)


def submit_and_poll(client, url: str, engine: str, timeout: int = 1800):
    """提交录音文件识别（说话人分离 2 人）并轮询返回 (detail, 耗时秒, 计费信息)。"""
    req = asr_models.CreateRecTaskRequest()
    req.EngineModelType = engine
    req.ChannelNum = 1
    req.SourceType = 0
    req.Url = url
    req.ResTextFormat = 2
    req.SpeakerDiarization = 1
    req.SpeakerNumber = 2
    req.ConvertNumMode = 3
    resp = client.CreateRecTask(req)
    task_id = resp.Data.TaskId
    t0 = time.monotonic()
    while True:
        q = asr_models.DescribeTaskStatusRequest()
        q.TaskId = task_id
        st = client.DescribeTaskStatus(q)
        status = st.Data.Status
        if status == 2:
            break
        if status == 3:
            raise RuntimeError(f"{engine} ASR 失败: {st.Data.ErrorMsg}")
        if time.monotonic() - t0 > timeout:
            raise RuntimeError(f"{engine} 轮询超时")
        time.sleep(5)
    elapsed = round(time.monotonic() - t0, 1)
    raw = st.Data.ResultStr if hasattr(st.Data, "ResultStr") and st.Data.ResultStr else st.Data.Result
    detail = _parse_detail(raw)
    return detail, elapsed


def _parse_detail(raw: str) -> list[dict]:
    """兼容两种返回：ResTextFormat=2 文本行 或 JSON ResultDetail（与 stages 一致）。"""
    import re
    try:
        result = json.loads(raw)
        return result.get("Response", result).get("ResultDetail", []) or []
    except (json.JSONDecodeError, AttributeError):
        pass
    detail = []
    for line in raw.splitlines():
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?),(\d+):(\d+(?:\.\d+)?),(\d+)\]\s*(.*)",
                     line.strip())
        if m and m.group(6).strip():
            start_sec = int(m.group(1)) * 60 + float(m.group(2))
            end_sec = int(m.group(3)) * 60 + float(m.group(4))
            detail.append({"StartMs": start_sec * 1000, "EndMs": end_sec * 1000,
                           "SpeakerId": int(m.group(5)),
                           "FinalSentence": m.group(6).strip()})
    return detail


def _fmt_sentences(detail) -> list[dict]:
    out = []
    for d in detail:
        text = d.get("FinalSentence") or d.get("SliceSentence") or ""
        if text.strip():
            out.append({"spk": d.get("SpeakerId"), "text": text.strip()})
    return out


def main(seg_key: str) -> None:
    cfg = load_cfg()
    store = CosStore(bucket=cfg["cos"]["bucket"], region=cfg["cos"]["region"],
                     secret_id=cfg["cos"]["secret_id"],
                     secret_key=cfg["cos"]["secret_key"])
    client = make_client(cfg)

    # 1) 下载源分段(48k) → 本地转 16k 单声道 → 上传 COS 临时 key
    local_src = str(Path(WORK) / "src.wav")
    store.download(seg_key, local_src)
    local_16k = str(Path(WORK) / "ab_16k.wav")
    import subprocess
    ffmpeg = _find_ffmpeg()
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", local_src,
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", local_16k],
                   check=True)
    tmp_key = "jiaopei/tmp/_asr_ab_16k.wav"
    store.put_file(tmp_key, local_16k)
    url = store.presigned_url(tmp_key, expired=3600)
    dur = Path(local_16k).stat().st_size / 2 / 16000 / 60
    print(f"样本: {seg_key}  16k 时长 {dur:.1f} 分钟, URL 已就绪")

    # 2) 两引擎各识别一次
    result = {"sample_key": seg_key, "minutes": round(dur, 2),
              "engine_compare": {}, "sentences": {}}
    for label, engine in ENGINES:
        print(f"---- 提交 {label} ({engine}) ----")
        detail, elapsed = submit_and_poll(client, url, engine)
        sents = _fmt_sentences(detail)
        text_all = "".join(s["text"] for s in sents)
        speakers = sorted({s["spk"] for s in sents})
        # 计费（元）：录音识别按实际音频时长，仅语音部分计费(此处按整段近似)
        fee = round(dur * (0.8 if "2.0" in engine else 1.75), 4)
        info = {
            "engine": engine, "elapsed_sec": elapsed,
            "sentence_count": len(sents),
            "char_count": len(text_all),
            "speakers": speakers,
            "est_cost_yuan": fee,
        }
        result["engine_compare"][engine] = info
        result["sentences"][engine] = sents
        print(f"  耗时 {elapsed}s, 句数 {len(sents)}, 字数 {len(text_all)}, "
              f"说话人 {speakers}, 计费约 ¥{fee}")
        # 打印前 8 句便于人工比对质量
        for s in sents[:8]:
            print(f"    [spk{s['spk']}] {s['text']}")
    # 3) 落盘
    out = Path(r"C:\recorder\reports\asr_ab_result.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nA/B 结果已写入 {out}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python asr_ab_test.py <cos_seg_key>")
        sys.exit(1)
    main(sys.argv[1])
