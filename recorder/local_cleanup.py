# -*- coding: utf-8 -*-
"""本地录音分段「安全回收」：仅删除已在 COS 确认上传的本地 wav。

判定依据（两级，越往后越严格）
  1. 课程级：COS 存在 <org>/<room>/<day>/<tag>_meta.json（meta 最后上传 = 整课就绪）
  2. 分片级（--verify-segments）：逐个分片 HEAD 远端对象且 Content-Length 与本地一致

安全闸门（默认全开，绝不因清理丢录音）
  - 录制中保护：组内最新分段 mtime 距今 < segment_seconds + 120 秒 → 跳过
  - 未确认上传的分组一律保留（只提示，不删）

用法
  python local_cleanup.py                              # 审计（dry-run），只打印不动文件
  python local_cleanup.py --apply                      # 删除「已确认上传」的本地分段
  python local_cleanup.py --apply --verify-segments    # 再逐分片校验远端存在+大小一致（更稳）
  python local_cleanup.py --apply --min-age-days 7     # 只清理 7 天前的（配合方案1保留期）
  python local_cleanup.py --apply --min-age-days 0 --verify-segments   # 一次性彻底回收
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qcloud_cos import CosConfig, CosS3Client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("local_cleanup")

DEFAULT_CFG = Path(r"C:\recorder\recorder\config.json")
# 形如 <course>_<YYYYMMDD>_<HHMMSS>_<5位序号>.wav
SEG_PAT = re.compile(r"^(.+_\d{8}_\d{6})_\d{5}\.wav$")


def group_local(out_dir: Path) -> dict[str, list[Path]]:
    """把 out_dir 下的分片 wav 按课程 tag 归组；不匹配的（如 mic_test.wav）忽略。"""
    groups: dict[str, list[Path]] = {}
    for wav in sorted(out_dir.glob("*.wav")):
        m = SEG_PAT.match(wav.name)
        if m:
            groups.setdefault(m.group(1), []).append(wav)
    return groups


def _day_of(tag: str) -> str:
    """与 uploader._object_key 保持一致：day = tag[-15:-7]。"""
    return tag[-15:-7]


def meta_key_for(org: str, room: str, tag: str) -> str:
    return f"{org}/{room}/{_day_of(tag)}/{tag}_meta.json"


def seg_key_for(org: str, room: str, tag: str, path: Path) -> str:
    """本地 <tag>_NNNNN.wav ↔ 远端 <org>/<room>/<day>/<tag>_NNNNN.wav。"""
    idx = path.stem.rsplit("_", 1)[-1]
    return f"{org}/{room}/{_day_of(tag)}/{tag}_{idx}.wav"


def build_client(cfg: dict) -> tuple[CosS3Client, str]:
    cos = cfg["cos"]
    client = CosS3Client(CosConfig(
        Region=cos["region"], SecretId=cos["secret_id"],
        SecretKey=cos["secret_key"], Timeout=30))
    return client, cos["bucket"]


def make_uploaded_checker(client: CosS3Client, bucket: str, org: str, room: str):
    """带缓存的 meta 存在性检查，避免同一 tag 多次请求。"""
    cache: dict[str, bool] = {}

    def is_uploaded(tag: str) -> bool:
        if tag not in cache:
            try:
                cache[tag] = bool(client.object_exists(
                    Bucket=bucket, Key=meta_key_for(org, room, tag)))
            except Exception as e:  # noqa: BLE001
                log.warning("查询 COS 失败 %s：%s（保守视为未上传）",
                            meta_key_for(org, room, tag), e)
                cache[tag] = False
        return cache[tag]

    return is_uploaded


def verify_group(client: CosS3Client, bucket: str, org: str, room: str,
                 tag: str, files: list[Path]) -> list[tuple[str, object, str]]:
    """逐分片校验远端存在且大小一致，返回问题列表 [(文件名, 本地大小, 远端情况)]。"""
    bad: list[tuple[str, object, str]] = []
    for f in files:
        key = seg_key_for(org, room, tag, f)
        local_sz = f.stat().st_size
        try:
            r = client.head_object(Bucket=bucket, Key=key)
            remote_sz = int(r.get("Content-Length", -1))
            if remote_sz != local_sz:
                bad.append((f.name, local_sz, f"远端 {remote_sz} 字节，大小不一致"))
        except Exception as e:  # noqa: BLE001
            bad.append((f.name, local_sz, f"远端缺失/异常: {str(e)[:70]}"))
    return bad


def delete_group(files: list[Path]) -> tuple[int, int]:
    """删除一组本地文件，返回 (成功数, 失败数)。"""
    ok = fail = 0
    for f in files:
        try:
            f.unlink()
            ok += 1
        except OSError as e:
            fail += 1
            log.warning("删除失败 %s：%s", f, e)
    return ok, fail


def plan_cleanup(cfg: dict, min_age_days: float, protect_recording: bool = True):
    """返回 (deletable, keep_unuploaded, skipped, unmatched)。"""
    out_dir = Path(cfg.get("out_dir", r"C:/recordings"))
    seg_sec = int(cfg.get("segment_seconds", 300))
    active_window = seg_sec + 120
    now = time.time()
    groups = group_local(out_dir)
    client, bucket = build_client(cfg)
    org, room = cfg.get("org_id", "org"), cfg["room_id"]
    is_uploaded = make_uploaded_checker(client, bucket, org, room)

    deletable, keep_unuploaded, skipped = [], [], []
    for tag, files in sorted(groups.items()):
        newest = max(f.stat().st_mtime for f in files)
        age_days = (now - newest) / 86400.0
        if protect_recording and (now - newest) < active_window:
            skipped.append((tag, len(files), age_days, "录制中"))
            continue
        if age_days < min_age_days:
            skipped.append((tag, len(files), age_days, f"未满 {min_age_days:g} 天"))
            continue
        if is_uploaded(tag):
            deletable.append((tag, files, age_days))
        else:
            keep_unuploaded.append((tag, len(files), age_days))

    unmatched = [w for w in out_dir.glob("*.wav") if not SEG_PAT.match(w.name)]
    return deletable, keep_unuploaded, skipped, unmatched


def main() -> int:
    ap = argparse.ArgumentParser(description="本地录音分段安全回收")
    ap.add_argument("--config", default=str(DEFAULT_CFG))
    ap.add_argument("--apply", action="store_true", help="实际删除（默认仅审计）")
    ap.add_argument("--min-age-days", type=float, default=0.0,
                    help="只清理该天数之前的（默认 0=不设年龄门槛）")
    ap.add_argument("--verify-segments", action="store_true",
                    help="逐分片校验远端存在且大小一致（更稳，请求较多）")
    ap.add_argument("--no-protect-recording", action="store_true",
                    help="关闭「录制中保护」（危险，一般不用）")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    org, room = cfg.get("org_id", "org"), cfg["room_id"]
    deletable, keep_unuploaded, skipped, unmatched = plan_cleanup(
        cfg, args.min_age_days, protect_recording=not args.no_protect_recording)

    # 可选：逐分片远端校验，剔除任何"对不上"的分组（宁可不删）
    verify_bad: list[tuple[str, list]] = []
    if args.verify_segments and deletable:
        client, bucket = build_client(cfg)
        print("逐分片校验远端对象…")
        passed = []
        for tag, files, age in deletable:
            bad = verify_group(client, bucket, org, room, tag, files)
            if bad:
                verify_bad.append((tag, bad))
            else:
                passed.append((tag, files, age))
        deletable = passed

    total_del_bytes = sum(f.stat().st_size for _, files, _ in deletable for f in files)
    total_del_files = sum(len(files) for _, files, _ in deletable)

    print("=" * 68)
    print(f"本地录音目录：{cfg.get('out_dir')}")
    print(f"模式：{'【实际删除】' if args.apply else '【审计 dry-run】'}   "
          f"年龄门槛：{args.min_age_days:g} 天  "
          f"分片校验：{'开' if args.verify_segments else '关'}")
    print("=" * 68)

    print(f"\n[可删除·已确认上传 COS]  {len(deletable)} 组 / {total_del_files} 文件 / "
          f"{total_del_bytes / 1048576:.1f} MB")
    for tag, files, age in deletable:
        sz = sum(f.stat().st_size for f in files) / 1048576
        print(f"  - {tag}  ({len(files)} 段, {sz:.1f} MB, {age:.1f} 天前)")

    if verify_bad:
        print(f"\n[⚠ 校验不通过·保留]  {len(verify_bad)} 组（远端不完整，绝不删）")
        for tag, bad in verify_bad:
            print(f"  - {tag}  ({len(bad)} 个分片对不上)")
            for name, sz, why in bad[:3]:
                print(f"      · {name}: {why}")

    if keep_unuploaded:
        print(f"\n[保留·COS 未见 meta（未上传/未完成）]  {len(keep_unuploaded)} 组")
        for tag, n, age in keep_unuploaded:
            print(f"  - {tag}  ({n} 段, {age:.1f} 天前)")

    if skipped:
        print(f"\n[跳过]  {len(skipped)} 组")
        for tag, n, age, why in skipped:
            print(f"  - {tag}  ({n} 段, {age:.1f} 天前) → {why}")

    if unmatched:
        print(f"\n[非课程文件·忽略]  {len(unmatched)} 个")
        for w in unmatched:
            print(f"  - {w.name}")

    if not args.apply:
        print("\n提示：以上为审计结果，未删除任何文件。确认后加 --apply 执行。")
        return 0

    ok_total = fail_total = 0
    for tag, files, _ in deletable:
        ok, fail = delete_group(files)
        ok_total += ok
        fail_total += fail
        log.info("[%s] 已删除本地 %d 段（失败 %d）", tag, ok, fail)
    print(f"\n完成：删除 {ok_total} 个文件"
          f"{'' if not fail_total else f'，失败 {fail_total}'}，"
          f"释放约 {total_del_bytes / 1048576:.1f} MB")
    return 0 if fail_total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
