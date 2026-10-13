"""音频上传：腾讯云 COS 分片直传 + 元数据 JSON（方案B · 传输层）。

- 分片上传（SDK 内部 multipart，8MB/片）：断网自动续传
- 上传完成后写 meta.json，云端以 meta.json 到达作为"整节课就绪"的信号
- 每段文件带 SHA256，供云端完整性校验
"""
import hashlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from qcloud_cos import CosConfig, CosS3Client

log = logging.getLogger("uploader")

# 上传进度状态文件：供监控面板（monitor_gui.py）实时读取
_STATUS_FILE = Path(r"C:/recorder/status/upload_status.json")


def _write_status(data: dict) -> None:
    """原子写状态 JSON，失败静默（绝不影响上传主流程）。"""
    try:
        _STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATUS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_STATUS_FILE)
    except Exception:  # noqa: BLE001
        pass


def _seg_progress(status: dict, seg_index: int, seg_name: str,
                  seg_bytes: int):
    """构造单个分段的字节级进度回调（限频写状态文件）。

    注意：必须返回**普通函数**。SDK 的 upload_file 会自行把它包进
    ProgressCallback；若传入 ProgressCallback 对象会被二次包装，
    线程池回调时报 'ProgressCallback' object is not callable，
    导致每个分片都失败（some upload_part fail after max_retry）。
    """
    last = {"t": 0.0}

    def on_progress(finished_size: int, file_size: int) -> None:
        now = time.monotonic()
        if now - last["t"] >= 0.5 or finished_size >= file_size:
            last["t"] = now
            status["current_segment"] = {
                "index": seg_index, "name": seg_name,
                "bytes": seg_bytes, "uploaded_bytes": finished_size,
            }
            status["updated_at"] = datetime.now().isoformat(timespec="seconds")
            _write_status(status)

    return on_progress


class Uploader:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        cos_cfg = cfg["cos"]
        self.bucket = cos_cfg["bucket"]
        self.client = CosS3Client(CosConfig(
            Region=cos_cfg["region"],
            SecretId=cos_cfg["secret_id"],
            SecretKey=cos_cfg["secret_key"],
            Timeout=300,  # 单个分片请求超时（秒）：家用上行慢，默认60s易超时
        ))

    def _object_key(self, room: str, lesson_tag: str, suffix: str) -> str:
        """统一对象命名：机构/教室/日期/课程tag_后缀"""
        org = self.cfg.get("org_id", "org")
        day = lesson_tag[-15:-7]                    # YYYYMMDD
        return f"{org}/{room}/{day}/{lesson_tag}_{suffix}"

    def upload_lesson(self, lesson, segments: list[Path]) -> list[str]:
        """上传全部分段 + 元数据 JSON，返回远端 key 列表。"""
        keys, total_bytes = [], 0
        tag = lesson.tag
        status = {
            "state": "uploading",
            "course_id": lesson.course_id,
            "tag": tag,
            "teacher": lesson.teacher,
            "total_segments": len(segments),
            "done_segments": 0,
            "total_bytes": sum(s.stat().st_size for s in segments),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        _write_status(status)
        try:
            for i, seg in enumerate(segments):
                key = self._object_key(lesson.room, tag, f"{i:05d}.wav")
                progress = _seg_progress(status, i, seg.name, seg.stat().st_size)
                # 分片上传：内部 multipart，自动断点续传；每段最多 5 次，指数退避
                # MAXThread=2：家用宽带上行有限，线程过多会互相挤占导致分片超时
                for attempt in range(1, 6):
                    try:
                        self.client.upload_file(
                            Bucket=self.bucket, Key=key, LocalFilePath=str(seg),
                            MAXThread=2, PartSize=8,  # PartSize 单位为 MB
                            progress_callback=progress)
                        break
                    except Exception as e:  # noqa: BLE001
                        log.warning("上传失败 %s 第%d次: %s", key, attempt, e)
                        if attempt == 5:
                            raise
                        time.sleep(min(2 ** attempt, 30))
                total_bytes += seg.stat().st_size
                keys.append(key)
                status["done_segments"] = i + 1
                status["updated_at"] = datetime.now().isoformat(timespec="seconds")
                _write_status(status)
                log.info("已上传 %s (%d bytes)", key, seg.stat().st_size)
        except Exception as e:  # noqa: BLE001
            status["state"] = "failed"
            status["error"] = str(e)
            status["updated_at"] = datetime.now().isoformat(timespec="seconds")
            _write_status(status)
            raise

        sr = int(self.cfg.get("sample_rate", 16000))
        # 教师归属直接取 lesson.teacher：课表触发 = 课表里的真实教师；
        # VAD / 手动触发 = 控制台「录课」页下拉栏选定的授课教师。
        # 云端不再做声纹识别，meta.teacher 就是课表匹配的唯一依据。
        meta = {
            "org_id": self.cfg.get("org_id"),
            "room_id": lesson.room,
            "course_id": lesson.course_id,
            "teacher": lesson.teacher,
            "start": lesson.start.isoformat(),
            "end": lesson.end.isoformat(),
            "duration_sec": int((lesson.end - lesson.start).total_seconds()),
            "audio_format": f"wav_{sr}k_16bit_mono",
            "segment_count": len(segments),
            "total_bytes": total_bytes,
            "segments": [
                {"key": k, "sha256": self._sha256(s), "bytes": s.stat().st_size}
                for k, s in zip(keys, segments)
            ],
            "uploaded_at": datetime.now().isoformat(),
        }
        meta_key = self._object_key(lesson.room, tag, "meta.json")
        try:
            self.client.put_object(
                Bucket=self.bucket, Key=meta_key,
                Body=json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            status["state"] = "failed"
            status["error"] = "元数据上传失败: " + str(e)
            status["updated_at"] = datetime.now().isoformat(timespec="seconds")
            _write_status(status)
            raise
        log.info("元数据已上传 %s segments=%d", meta_key, len(segments))
        status["state"] = "done"
        status["finished_at"] = datetime.now().isoformat(timespec="seconds")
        status["updated_at"] = status["finished_at"]
        _write_status(status)
        return keys

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
