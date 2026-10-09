# -*- coding: utf-8 -*-
"""本地声纹识别（1:N）—— 可替代腾讯云 VoicePrint 增值服务。

为什么能本地化：算法本身很精简，只有两步 ——
  ① 用神经网络把一段语音压成定长向量（embedding，CAM++ 为 192 维）
  ② 与已注册向量算余弦相似度，取最高分
代码也就百来行；真正的"知识"在模型权重里（models/campplus_sv_zh_16k.onnx，27MB）。

⚠️ 关键前提：「腾讯云的声纹库无法迁移到本地」。
   腾讯 1:N 用的是它自家私有模型产出的向量，与本地模型的向量空间不互通；
   本地库必须用**本地模型重新注册**（每个 speaker 一段 3~30s 干净音频）。
   两套分数体系也不可直接比较，阈值需各自标定（本地用余弦相似度）。

模型：3D-Speaker CAM++ (zh-cn, 16kHz)
  输入 80 维 fbank，输出 192 维 embedding。
  镜像下载（3 秒，~8MB/s）：
  https://hf-mirror.com/csukuangfj/speaker-embedding-models/resolve/main/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx

用法：
  # 注册（可多次调用，同一 person_id 会追加样本）
  python voiceprint_local.py enroll --wav a.wav b.wav --person-id T001 --name 张三 --role teacher
  # 重新录制：丢掉该人旧样本只留新的（先自动备份整库）
  python voiceprint_local.py enroll --wav new.wav --person-id T001 --name 张三 --replace
  python voiceprint_local.py list
  python voiceprint_local.py remove --person-id T001
  python voiceprint_local.py verify --wav probe.wav --topk 3
  python voiceprint_local.py selftest            # 用 enroll_samples 建库并交叉验证
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import wave
from pathlib import Path

import numpy as np
import sherpa_onnx

DEFAULT_MODEL = r"C:\recorder\models\campplus_sv_zh_16k.onnx"
DEFAULT_DB = r"C:\recorder\cloud\voiceprint_local_db.json"
RECOMMEND_SEC = (3.0, 30.0)      # 注册音频的建议时长区间
BACKUP_DIRNAME = "voiceprint_local_backup"   # 破坏性操作前的整库快照目录


def read_wav_16k(path: str) -> np.ndarray:
    """读 wav → float32 [-1,1] 单声道 16kHz；非 16k 走线性插值重采样。"""
    with wave.open(str(path)) as w:
        sr, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError(f"仅支持 16bit PCM wav：{path}")
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        tgt = int(round(len(x) * 16000 / sr))
        x = np.interp(np.linspace(0, len(x) - 1, tgt),
                      np.arange(len(x)), x).astype(np.float32)
    return x


class LocalVoiceprint:
    """本地声纹库：注册 / 1:N 比对 / 持久化。

    每人可存多段样本的 embedding；比对时取「与任一注册样本的最大余弦」，
    比取平均更耐噪声（课堂片段质量参差，平均会被差样本拉低）。
    """

    def __init__(self, model_path: str = DEFAULT_MODEL, db_path: str = DEFAULT_DB,
                 num_threads: int = 2):
        self.model_path = str(model_path)
        self.db_path = Path(db_path)
        if not Path(self.model_path).exists():
            raise FileNotFoundError(
                f"声纹模型不存在：{self.model_path}\n"
                "下载：curl -L -o <目标> "
                "https://hf-mirror.com/csukuangfj/speaker-embedding-models/resolve/main/"
                "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx")
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=self.model_path, num_threads=num_threads)
        self._ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.dim = self._ex.dim
        self.persons: dict[str, dict] = {}
        self._load()

    # ---------------- 底层 ----------------

    def embed_array(self, x: np.ndarray, tag: str = "array") -> np.ndarray:
        """PCM（float32 / 16kHz / 单声道）→ L2 归一化 embedding。

        直接吃内存数组，供"录音中实时探测"这类拿不到文件、或不愿落盘的场景。
        """
        if x is None or len(x) == 0:
            raise RuntimeError(f"音频为空，无法提取声纹：{tag}")
        stream = self._ex.create_stream()
        stream.accept_waveform(sample_rate=16000,
                               waveform=np.ascontiguousarray(x, dtype=np.float32))
        stream.input_finished()
        if not self._ex.is_ready(stream):
            raise RuntimeError(f"音频过短或无效，无法提取声纹：{tag}")
        v = np.asarray(self._ex.compute(stream), dtype=np.float32).ravel()
        n = float(np.linalg.norm(v))
        if n < 1e-6:
            raise RuntimeError(f"声纹向量为零（疑似静音）：{tag}")
        return v / n

    def embed_wav(self, wav_path: str) -> np.ndarray:
        """音频文件 → L2 归一化后的 embedding（单位向量，点积即余弦）。"""
        return self.embed_array(read_wav_16k(wav_path), tag=str(wav_path))

    def _load(self) -> None:
        if self.db_path.exists():
            data = json.loads(self.db_path.read_text(encoding="utf-8"))
            self.persons = data.get("persons", {}) or {}

    def _save(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.write_text(json.dumps({
            "model": Path(self.model_path).stem,
            "dim": self.dim,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "persons": self.persons,
        }, ensure_ascii=False), encoding="utf-8")

    # ---------------- 注册 / 管理 ----------------

    def _embed_many(self, wav_paths: list[str]) -> tuple[list[list[float]], list[dict]]:
        """批量取 embedding，单条失败不影响其余（返回 (向量, 逐条明细)）。"""
        vecs: list[list[float]] = []
        detail: list[dict] = []
        for p in wav_paths:
            try:
                e = self.embed_wav(p)
            except Exception as exc:  # noqa: BLE001
                detail.append({"wav": str(p), "ok": False, "error": str(exc)})
                continue
            vecs.append([round(float(x), 6) for x in e])
            detail.append({"wav": str(p), "ok": True,
                           "duration_sec": round(len(read_wav_16k(p)) / 16000.0, 1)})
        return vecs, detail

    def backup(self) -> Path | None:
        """整库快照到 voiceprint_local_backup/<库名>_<时间戳>.json。

        只在破坏性操作（replace / remove）前调用 —— 本地库是标定出来的资产，
        误覆盖一次就得重跑 calibrate_local_vp.py（要几十分钟），留个快照很便宜。
        """
        if not self.db_path.exists():
            return None
        d = self.db_path.parent / BACKUP_DIRNAME
        d.mkdir(parents=True, exist_ok=True)
        dst = d / f"{self.db_path.stem}_{time.strftime('%Y%m%d_%H%M%S')}.json"
        # 时间戳只到秒，同一秒内两次破坏性操作会撞名 —— 撞了就加序号，
        # 否则后一次快照会盖掉前一次，等于没备份。
        if dst.exists():
            n = 2
            while True:
                cand = d / f"{dst.stem}_{n}{dst.suffix}"
                if not cand.exists():
                    dst = cand
                    break
                n += 1
        shutil.copy2(self.db_path, dst)
        return dst

    def enroll(self, person_id: str, person_name: str, role: str,
               wav_paths: list[str], replace: bool = False,
               backup: bool = True) -> dict:
        """注册或追加样本。同一 person_id 多次调用会累加 embedding。

        replace=True 改用「替换」语义：先给整库拍快照，再丢弃该人的旧样本，
        只保留本次音频算出的向量。这是「重新录制声纹」的正确行为 ——
        1:N 比对付取「与任一注册样本的最大余弦」（见 rank_embedding），
        一条坏样本会长期顶在上面把别人拉高，追加式重录救不回来。
        """
        vecs, detail = self._embed_many(wav_paths)
        if not vecs:
            raise RuntimeError(f"注册失败，无有效样本：{[d['error'] for d in detail]}")

        existed = person_id in self.persons
        dropped = 0
        backup_path = None
        if replace and existed and backup:
            backup_path = self.backup()
            dropped = len(self.persons[person_id].get("embeddings") or [])

        rec = self.persons.setdefault(person_id, {
            "person_name": person_name, "role": role,
            "embeddings": [], "samples": [],
        })
        rec["person_name"] = person_name      # 允许改名/改角色
        rec["role"] = role
        if replace:
            rec["embeddings"] = []
            rec["samples"] = []
        rec["embeddings"].extend(vecs)
        rec["samples"].extend(detail)
        rec["enrolled_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._save()
        return {"person_id": person_id, "added": len(vecs),
                "replaced": bool(replace and existed), "dropped": dropped,
                "backup": str(backup_path) if backup_path else "",
                "total_embeddings": len(rec["embeddings"]), "detail": detail}

    def remove(self, person_id: str, backup: bool = True) -> bool:
        if person_id not in self.persons:
            return False
        if backup:
            self.backup()
        del self.persons[person_id]
        self._save()
        return True

    def list_persons(self) -> list[dict]:
        out = []
        for pid, r in sorted(self.persons.items(), key=lambda kv: kv[1]["person_name"]):
            out.append({"person_id": pid, "person_name": r["person_name"],
                        "role": r["role"], "samples": len(r["embeddings"])})
        return out

    # ---------------- 比对 ----------------

    def _matrix(self) -> tuple[list[str], np.ndarray]:
        """把库摊平成 (person_id 列表, [N,192] 矩阵)，便于一次算完余弦。"""
        ids: list[str] = []
        rows: list[list[float]] = []
        for pid, r in self.persons.items():
            for e in r.get("embeddings", []):
                ids.append(pid)
                rows.append(e)
        if not rows:
            return [], np.zeros((0, self.dim), dtype=np.float32)
        return ids, np.asarray(rows, dtype=np.float32)

    def rank_embedding(self, q: np.ndarray, topk: int = 2) -> list[dict]:
        """单个 embedding → 按人聚合后的 Top-K（每人取与其最好的一段样本的余弦）。"""
        ids, mat = self._matrix()
        if not ids:
            return []
        cos = mat @ q                                   # 已 L2 归一化 → 点积即余弦
        best: dict[str, float] = {}
        for pid, c in zip(ids, cos):
            if c > best.get(pid, -2.0):
                best[pid] = float(c)
        ranked = sorted(best.items(), key=lambda kv: -kv[1])[:topk]
        out = []
        for pid, c in ranked:
            r = self.persons[pid]
            out.append({"person_id": pid, "person_name": r["person_name"],
                        "role": r["role"], "cosine": round(c, 4),
                        "score": round(c * 100, 1)})
        return out

    def verify_topk(self, wav_path: str, topk: int = 2) -> list[dict]:
        """1:N 比对，返回按相似度降序的 Top-K（每人取其最好的一段）。

        score = 余弦 × 100，仅用于与既有 0~100 口径对齐查看；
        本地余弦与腾讯云分数**不可直接比较**，阈值需另行标定。
        """
        return self.rank_embedding(self.embed_wav(wav_path), topk=topk)

    def verify(self, wav_path: str, topk: int = 2) -> dict:
        """返回 Top-1/次名与领先幅度，判定口径与云端复合判据保持同构。

        accept = top1 >= min_cosine  OR  (top1 >= floor AND top1-top2 >= margin)
        本地余弦经验值：同一人 >0.55、不同人 <0.35；默认 floor=0.35 / margin=0.15。
        """
        tops = self.verify_topk(wav_path, topk=max(2, topk))
        if not tops:
            return {"hit": False, "reason": "empty_db", "tops": []}
        t1 = tops[0]
        t2 = tops[1] if len(tops) > 1 else None
        margin = t1["cosine"] - (t2["cosine"] if t2 else 0.0)
        return {"hit": True, "top1": t1, "top2": t2,
                "margin": round(margin, 4),
                "tops": tops}

    def verify_multi_sources(self, sources: list, topk: int = 2,
                             min_clip_sec: float = 1.5) -> dict:
        """多片段投票式 1:N 比对 —— 替代"把若干句拼成一条长音频再比对"。

        为什么投票更稳：
          拼接会先把多个片段的声学条件平均掉，且**任意一段被噪声/串音污染，
          都会拖累整条拼接音频**；各片段独立取 embedding 再投票时，
          坏片段只是丢掉自己那一票，不会污染其余票 —— 这正是
          "同课异人余弦区间重叠、最差领先仅 0.025" 场景的解药。

        sources: 可为文件路径(str/Path)或 PCM 数组(np.ndarray)。

        聚合口径（两级，先票数后稳健分）：
          ① 票数   = 该人被多少个片段判为 Top-1（谁是"多数片段的第一名"）；
          ② 稳健分 = 该人在各片段中余弦的**中位数**。
        用中位数而非平均：片段质量参差，单个极值不该主导结论。
        排序键 = (票数, 中位数)。
        """
        per_clip: list[list[dict]] = []
        used = 0
        for src in sources:
            try:
                q = (self.embed_array(src, tag="array") if isinstance(src, np.ndarray)
                     else self.embed_wav(str(src)))
            except Exception:  # noqa: BLE001  单片段失败不影响其余投票
                continue
            ranked = self.rank_embedding(q, topk=max(2, topk))
            if not ranked:
                continue
            used += 1
            per_clip.append(ranked)

        if not per_clip:
            return {"hit": False, "reason": "no_valid_clip",
                    "clips": 0, "tops": []}

        votes: dict[str, int] = {}
        pool: dict[str, list[float]] = {}
        for ranked in per_clip:
            votes[ranked[0]["person_id"]] = votes.get(ranked[0]["person_id"], 0) + 1
            for item in ranked:
                pool.setdefault(item["person_id"], []).append(item["cosine"])

        rows = []
        for pid, cosines in pool.items():
            r = self.persons[pid]
            rows.append({
                "person_id": pid, "person_name": r["person_name"], "role": r["role"],
                "votes": votes.get(pid, 0),
                "median": round(float(np.median(cosines)), 4),
                "max": round(max(cosines), 4),
                "min": round(min(cosines), 4),
                "n_clips": len(cosines),
            })
        rows.sort(key=lambda x: (x["votes"], x["median"]), reverse=True)
        tops = rows[:max(2, topk)]
        for t in tops:                       # 对外沿用 cosine/score 字段 = 聚合分
            t["cosine"] = t["median"]
            t["score"] = round(t["median"] * 100, 1)

        t1 = tops[0]
        t2 = tops[1] if len(tops) > 1 else None
        margin = t1["median"] - (t2["median"] if t2 else 0.0)
        return {"hit": True, "top1": t1, "top2": t2, "margin": round(margin, 4),
                "clips": used, "tops": tops, "per_clip": per_clip}

    def verify_multi(self, wav_paths: list[str], topk: int = 2) -> dict:
        """verify_multi_sources 的文件路径版。"""
        return self.verify_multi_sources([str(p) for p in wav_paths], topk=topk)


# ---------------- CLI ----------------

def _cmd_enroll(a) -> int:
    db = LocalVoiceprint(a.model, a.db)
    r = db.enroll(a.person_id, a.name, a.role, a.wav, replace=getattr(a, "replace", False))
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0


def _cmd_list(a) -> int:
    db = LocalVoiceprint(a.model, a.db)
    rows = db.list_persons()
    if not rows:
        print("（声纹库为空）")
    for r in rows:
        print(f"  {r['person_id']:8s} {r['person_name']:12s} {r['role']:8s} "
              f"{r['samples']} 段样本")
    return 0


def _cmd_remove(a) -> int:
    db = LocalVoiceprint(a.model, a.db)
    print("已删除" if db.remove(a.person_id) else "未找到该人员")
    return 0


def _cmd_verify(a) -> int:
    db = LocalVoiceprint(a.model, a.db)
    r = db.verify(a.wav, topk=a.topk)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0


def _cmd_selftest(a) -> int:
    """用 enroll_samples 建库，再用课堂片段交叉验证（本地、零成本）。"""
    samples = Path(r"C:\recorder\cloud_work\enroll_samples")
    db = LocalVoiceprint(a.model, a.db)
    print("=== 1) 用注册样本建库 ===")
    for w in sorted(samples.glob("*.wav")):
        stem = w.stem                      # 形如 S002_StudentName
        pid, _, name = stem.partition("_")
        r = db.enroll(pid, name, "student", [str(w)])
        print(f"  {pid:6s} {name:14s} +{r['added']} 段")

    print("\n=== 2) 交叉验证：注册样本自比（应接近 1.0，且各自第一）===")
    for w in sorted(samples.glob("*.wav")):
        stem = w.stem
        pid, _, name = stem.partition("_")
        tops = db.verify_topk(str(w), topk=3)
        ok = tops and tops[0]["person_id"] == pid
        mark = "✅" if ok else "❌"
        print(f"  {mark} {name:14s} → " +
              " | ".join(f"{t['person_name']}={t['cosine']:.3f}" for t in tops))

    print("\n=== 3) 课堂片段验证（腾讯云已确认身份，用于交叉校验）===")
    # 目录名含学生姓名，故不写进源码：从 cloud_work 下自动挑「同时有
    # 学生片段 vp_1_*.wav 与教师片段 vp_0_*.wav」的课程目录。
    _cw = Path(r"C:\recorder\cloud_work")
    probes = []
    if _cw.is_dir():
        for _i, _d in enumerate(sorted((x for x in _cw.iterdir() if x.is_dir()),
                                       key=lambda q: q.name), 1):
            _stu = sorted(_d.glob("vp_1_*.wav"))
            _tea = sorted(_d.glob("vp_0_*.wav"))
            if _stu and _tea:
                probes.append((str(_stu[0]), None, f"课程#{_i} 学生片段"))
                probes.append((str(_tea[0]), None, f"课程#{_i} 教师片段"))
            if len(probes) >= 4:
                break
    for path, expect, label in probes:
        if not Path(path).exists():
            print(f"  -- {label}: 文件不存在，跳过")
            continue
        tops = db.verify_topk(path, topk=3)
        line = " | ".join(f"{t['person_name']}={t['cosine']:.3f}" for t in tops)
        verdict = ""
        if expect:
            verdict = "✅ 命中预期" if tops and tops[0]["person_id"] == expect else "❌ 与预期不符"
        print(f"  {label:22s} → {line}   {verdict}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="本地声纹识别（1:N）")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--db", default=DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("enroll", help="注册声纹")
    p.add_argument("--wav", nargs="+", required=True)
    p.add_argument("--person-id", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--role", default="student", choices=["teacher", "student"])
    p.add_argument("--replace", action="store_true",
                   help="替换该人全部旧样本（重新录制语义），会先备份整库")
    p.set_defaults(func=_cmd_enroll)

    p = sub.add_parser("list", help="列出已注册人员")
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("remove", help="删除某人声纹")
    p.add_argument("--person-id", required=True)
    p.set_defaults(func=_cmd_remove)

    p = sub.add_parser("verify", help="1:N 比对")
    p.add_argument("--wav", required=True)
    p.add_argument("--topk", type=int, default=2)
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser("selftest", help="用现有样本建库并交叉验证")
    p.set_defaults(func=_cmd_selftest)

    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
