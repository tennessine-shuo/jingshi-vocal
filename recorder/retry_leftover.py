# -*- coding: utf-8 -*-
"""本地残留分段补传（可在监控面板外独立运行）：
- 已在 COS 上存在 meta.json 的课程：本地 wav 视为已成功上传，直接删除本地文件
- 其余课程：按 tag 分组走 uploader.upload_lesson 上传（分段 + meta.json）
用法: python retry_leftover.py
"""
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))

from qcloud_cos import CosConfig, CosS3Client
from uploader import Uploader

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("retry_leftover")

CFG_PATH = Path(r"C:\recorder\recorder\config.json")
PAT = re.compile(r"^(.+_\d{8}_\d{6})_\d{5}\.wav$")


def main() -> None:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    out_dir = Path(cfg.get("out_dir", r"C:/recordings"))
    client = CosS3Client(CosConfig(
        Region=cfg["cos"]["region"], SecretId=cfg["cos"]["secret_id"],
        SecretKey=cfg["cos"]["secret_key"], Timeout=60))
    bucket = cfg["cos"]["bucket"]
    org = cfg.get("org_id", "org")
    room = cfg["room_id"]

    groups: dict[str, list[Path]] = {}
    for wav in sorted(out_dir.glob("*.wav")):
        m = PAT.match(wav.name)
        if m:
            groups.setdefault(m.group(1), []).append(wav)
    log.info("发现 %d 组残留分段", len(groups))

    # 仍在录制的课程（最近一个分段仍在写入）绝不能动：
    # 上传会拷走未写完的文件，删除会破坏课程结束时的整课上传统一性
    seg_sec = int(cfg.get("segment_seconds", 300))
    active_window = seg_sec + 120
    now = time.time()

    for tag, segs in sorted(groups.items()):
        newest = max(s.stat().st_mtime for s in segs)
        if now - newest < active_window:
            log.info("[%s] 最近分段仍在写入（%.0f 秒前），判定为录制中，跳过",
                     tag, now - newest)
            continue
        day = tag[-15:-7]
        meta_key = f"{org}/{room}/{day}/{tag}_meta.json"
        if client.object_exists(Bucket=bucket, Key=meta_key):
            log.info("[%s] COS 已有 meta，删除本地 %d 个分段", tag, len(segs))
            for s in segs:
                try:
                    s.unlink()
                except OSError as e:
                    log.warning("删除失败 %s: %s", s, e)
            continue

        log.info("[%s] 开始上传 %d 个分段（%.1f MB）", tag, len(segs),
                 sum(s.stat().st_size for s in segs) / 1048576)
        parts = tag.rsplit("_", 2)
        start = datetime.strptime(parts[1] + "_" + parts[2], "%Y%m%d_%H%M%S")
        end = datetime.fromtimestamp(max(s.stat().st_mtime for s in segs))
        lesson = SimpleNamespace(course_id=parts[0], teacher="补传", tag=tag,
                                 start=start, end=end, room=room)
        up = Uploader(cfg)
        try:
            up.upload_lesson(lesson, segs)
        except Exception as e:  # noqa: BLE001
            log.error("[%s] 上传失败: %s（保留本地文件，可再次运行本脚本）", tag, e)
            continue
        log.info("[%s] 上传完成，删除本地分段", tag)
        for s in segs:
            try:
                s.unlink()
            except OSError as e:
                log.warning("删除失败 %s: %s", s, e)
        time.sleep(1)
    log.info("补传脚本结束")


if __name__ == "__main__":
    main()
