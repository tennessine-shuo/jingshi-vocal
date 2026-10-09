"""补传工具：上传某节课已录制但未能上传的本地分段。

用法：python retry_upload.py --config config.json --course TEST-001 --start "2026-09-04T19:46:00" --end "2026-09-04T19:56:00"
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

from recorder import Lesson
from uploader import Uploader


def main() -> None:
    ap = argparse.ArgumentParser(description="补传本节课的本地分段")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--course", required=True, help="course_id，如 TEST-001")
    ap.add_argument("--start", required=True, help="ISO 格式，如 2026-09-04T19:46:00")
    ap.add_argument("--end", required=True, help="ISO 格式")
    ap.add_argument("--teacher", default="补传")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    lesson = Lesson(
        course_id=args.course,
        teacher=args.teacher,
        start=datetime.fromisoformat(args.start),
        end=datetime.fromisoformat(args.end),
        room=cfg["room_id"],
    )
    out_dir = Path(cfg["out_dir"])
    segs = sorted(out_dir.glob(lesson.tag + "_*.wav"))
    if not segs:
        print(f"未找到分段：{out_dir}/{lesson.tag}_*.wav")
        return
    print(f"找到 {len(segs)} 个分段，开始补传 {lesson.tag} ...")
    keys = Uploader(cfg).upload_lesson(lesson, segs)
    print(f"补传完成，共 {len(keys)} 个对象：")
    for k in keys:
        print("  cos://" + cfg["cos"]["bucket"] + "/" + k)
    print("  cos://" + cfg["cos"]["bucket"] + "/" +
          f"{cfg.get('org_id', 'org')}/{lesson.room}/{lesson.tag[-15:-7]}/{lesson.tag}_meta.json")


if __name__ == "__main__":
    main()
