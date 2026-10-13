"""录课终端主程序：双触发自动录制（课表 + VAD 人声检测）+ 上传（方案B · 教室端）。

运行环境：教室录课终端（Windows/Linux 迷你主机），Python 3.10+。
依赖：ffmpeg 已安装并加入 PATH；cos-python-sdk-v5；启用 VAD 时还需 sounddevice/numpy。
用法：python recorder.py --config config.json

触发模型（两种触发是"或"关系，课表优先）：
  1. 课表触发：开课前 3 分钟自动开始，课表到点强制停止
  2. VAD 触发：检测到人声连续 >= vad_start_sec 秒自动开始；
      静音连续 >= vad_stop_sec 秒自动停止（仅对 VAD 课生效）
VAD 课以 "VAD-" 前缀标识，最长录制 vad_max_sec 秒防失控；
VAD 课进行中若课表课即将开始，自动切换为课表课。

授课教师（2026-10-13 起）：课表归属不再依赖声纹识别 —— 由控制台
「录课」页下拉栏手动选择授课教师（写入 config.json 的 selected_teacher，
支持热切换）。VAD 触发的临时课以所选教师上报，云端按该教师的
飞书课表匹配课程归属。
"""
import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# 复用 cloud/ 下的本地声纹与飞书课表模块（recorder/ 与 cloud/ 同级）。
# 必须在依赖它们的 import 之前注入 sys.path，保证任意工作目录下都能解析。
_CLOUD_DIR = Path(__file__).resolve().parent.parent / "cloud"
if str(_CLOUD_DIR) not in sys.path:
    sys.path.insert(0, str(_CLOUD_DIR))

from audio_stream import find_ffmpeg
from local_cleanup import delete_group, group_local, make_uploaded_checker
from uploader import Uploader
from vad import VadDetector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# 文件日志：开机自启动后没有控制台窗口，日志落盘供排查（滚动保留 5×5MB）
from logging.handlers import RotatingFileHandler

_LOG_DIR = Path(r"C://recorder//logs")
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_fh = RotatingFileHandler(_LOG_DIR / "recorder.log", maxBytes=5 * 1024 * 1024,
                          backupCount=5, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.getLogger().addHandler(_fh)

log = logging.getLogger("recorder")

# 状态文件：供监控面板（monitor_gui.py）实时读取录制状态
_STATUS_DIR = Path(r"C:/recorder/status")
# 手动控制指令文件：监控面板写入 {"action": "start"|"stop", ...}，主循环每秒消费
_CONTROL_FILE = _STATUS_DIR / "control.json"


def _write_json_atomic(path: Path, data: dict) -> None:
    """原子写 JSON（先写临时文件再替换），避免监控端读到半截文件。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001 状态写失败绝不影响录制主流程
        pass


class Lesson:
    """一次课程的录制任务。"""

    def __init__(self, course_id: str, teacher: str, start: datetime, end: datetime, room: str):
        self.course_id = course_id
        self.teacher = teacher
        self.start = start
        self.end = end
        self.room = room

    @property
    def tag(self) -> str:
        return self.course_id + "_" + self.start.strftime("%Y%m%d_%H%M%S")

    @property
    def is_vad(self) -> bool:
        """是否由 VAD 触发（无课表信息的临时课）。"""
        return self.course_id.startswith("VAD-")

    @property
    def trigger_type(self) -> str:
        """触发方式：课表 / VAD / 手动。"""
        if self.is_vad:
            return "VAD"
        if self.course_id.startswith("MANUAL-"):
            return "手动"
        return "课表"


class Recorder:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.uploader = Uploader(cfg)
        self.out_dir = Path(cfg["out_dir"])
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.proc: subprocess.Popen | None = None
        self._ff_log = None  # ffmpeg stderr 的文件句柄（见 start_lesson）
        self.current: Lesson | None = None
        self.vad: VadDetector | None = None
        self._last_sweep = 0.0  # 上次本地回收的时间（monotonic），用于限频
        # ---- 音频设备（控制台「麦克风」页热切换）----
        self._audio_busy = False          # 正在重载设备时置位，避免并发重入
        self._audio_note: dict | None = None   # 最近一次设备操作的结果（回显给控制台）

    # ---------- 状态输出（供监控面板） ----------
    def write_status(self) -> None:
        """写当前状态到 C:/recorder/status/recorder_status.json（每秒一次）。"""
        state = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "state": "recording" if self.current is not None else "idle",
            "vad_enabled": self.vad is not None,
            # 控制台要按 PID 停/重启服务；不能从 *.lock 读 —— 锁文件首字节被
            # msvcrt 独占锁定，运行期间外部读到的是 PermissionError。
            "pid": os.getpid(),
            # 控制台「录课」页选择的授课教师（课表归属的依据）
            "selected_teacher": (self.cfg.get("selected_teacher") or "").strip(),
        }
        state["audio"] = self._audio_brief()
        if self.current is not None:
            segs = sorted(self.out_dir.glob(self.current.tag + "_*.wav"))
            state["lesson"] = {
                "course_id": self.current.course_id,
                "teacher": self.current.teacher,
                "tag": self.current.tag,
                "start": self.current.start.isoformat(timespec="seconds"),
                "end": self.current.end.isoformat(timespec="seconds"),
                "is_vad": self.current.is_vad,
                "trigger": self.current.trigger_type,
                "segments": len(segs),
                "bytes": sum(s.stat().st_size for s in segs),
            }
        _write_json_atomic(_STATUS_DIR / "recorder_status.json", state)

    def _audio_brief(self) -> dict:
        """当前采集设备 + 实时电平（控制台「麦克风」页的数据源）。

        电平统计复用 VAD 这一路已有采集，不在控制台另开一路麦克风；
        换麦克风前后可直接对比同一指标（中位/p95），判断新麦是否需要重校。
        """
        dev = str(self.cfg.get("audio_device") or "")
        info: dict = {
            "device": dev,
            "device_name": dev.split("=", 1)[-1],
            "fmt": self.cfg.get("audio_fmt", "dshow"),
            "voice_rms": self.cfg.get("voice_rms"),
            "vad_active": bool(self.vad is not None and self.vad.is_alive()),
            "busy": self._audio_busy,
        }
        if self.vad is not None and getattr(self.vad, "started_at", 0):
            info["uptime_sec"] = int(time.time() - self.vad.started_at)
            info["level"] = self.vad.level_stats(10.0)
        if self._audio_note:
            info["note"] = self._audio_note
        return info

    # ---------- 课表 ----------
    def fetch_lessons(self, day: datetime) -> list[Lesson]:
        """拉取当日课表。
        骨架实现：读取本地 lessons.json；正式环境替换为教务系统/飞书日历 API。
        """
        path = Path(self.cfg["schedule_file"])
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        lessons = []
        for item in data:
            start = datetime.fromisoformat(item["start"])
            if start.date() != day.date():
                continue
            lessons.append(Lesson(
                course_id=item["course_id"],
                teacher=item["teacher"],
                start=start,
                end=datetime.fromisoformat(item["end"]),
                room=item.get("room", self.cfg["room_id"]),
            ))
        lessons.sort(key=lambda x: x.start)
        return lessons

    def _pending_lesson(self, now: datetime) -> Lesson | None:
        """课表上"即将开始"的课：开课前 3 分钟内，且不早于 30 分钟前。"""
        candidate = None
        for lesson in self.fetch_lessons(now):
            delta = (lesson.start - now).total_seconds()
            if -1800 <= delta <= 180:
                if candidate is None or delta < (candidate.start - now).total_seconds():
                    candidate = lesson
        return candidate

    # ---------- 录制 ----------
    def start_lesson(self, lesson: Lesson) -> None:
        """启动 ffmpeg 分段录制（16kHz/16bit/单声道，每 segment_seconds 一段 WAV）。

        采样率由 config.sample_rate 控制（默认 16000）。云端 ASR/声纹统一按
        16kHz 处理，源文件用 16k 直出可省一次重采样并把上传/存储降到 1/3。
        """
        device = self.cfg["audio_device"]           # 例: audio=USB MIC（Windows dshow）
        fmt = self.cfg.get("audio_fmt", "dshow")    # 或 alsa/pulse（Linux）
        seg_sec = self.cfg.get("segment_seconds", 300)
        sample_rate = int(self.cfg.get("sample_rate", 16000))
        prefix = self.out_dir / lesson.tag
        cmd = [
            find_ffmpeg(), "-y", "-loglevel", "warning",
            "-f", fmt, "-i", device,
            "-c:a", "pcm_s16le", "-ar", str(sample_rate), "-ac", "1",
            "-f", "segment", "-segment_time", str(seg_sec), "-reset_timestamps", "1",
            str(prefix) + "_%05d.wav",
        ]
        log.info("开始录制 course=%s teacher=%s 触发=%s",
                 lesson.course_id, lesson.teacher, lesson.trigger_type)
        # ffmpeg 的 stderr 落盘而**不是** PIPE：
        #   ① PIPE 从不读取 → 缓冲区（64KB）写满后 ffmpeg 阻塞，录制静默卡死；
        #   ② text=True 在没有 encoding 时按 UTF-8 解码 GBK 输出 → 抛错。
        # 写文件既不会阻塞，又保住了排查"录到静音 / 设备被抢占"所需的原始日志。
        self._close_ff_log()
        self._ff_log = open(self.out_dir / (lesson.tag + "_ffmpeg.log"),
                            "ab", buffering=0)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                     stderr=self._ff_log,
                                     creationflags=(subprocess.CREATE_NO_WINDOW
                                                    if os.name == "nt" else 0))
        self.current = lesson

    def _close_ff_log(self) -> None:
        """关闭上一课的 ffmpeg 日志句柄（start_lesson / stop_recording 都会调）。"""
        if self._ff_log is not None:
            try:
                self._ff_log.close()
            except OSError:
                pass
            self._ff_log = None

    def start_vad_lesson(self, now: datetime) -> None:
        """按 VAD 触发创建临时课并开始录制。

        教师取控制台选定的授课教师（selected_teacher）：云端课表匹配
        完全依赖 meta 里的这个字段，不再做声纹识别兜底。
        """
        lesson = Lesson(
            course_id="VAD-" + now.strftime("%Y%m%d_%H%M%S"),
            teacher=(self.cfg.get("selected_teacher") or "").strip() or "未知",
            start=now,
            end=now + timedelta(seconds=self.cfg.get("vad_max_sec", 7200)),
            room=self.cfg["room_id"],
        )
        self.start_lesson(lesson)

    def stop_recording(self) -> list[Path]:
        """停止录制并返回分段文件列表。"""
        if self.proc is None or self.current is None:
            return []
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self._close_ff_log()
        segs = sorted(self.out_dir.glob(self.current.tag + "_*.wav"))
        if not segs:
            log.warning("未生成任何分段，课程可能未录到音频")
        else:
            log.info("录制结束 course=%s segments=%d", self.current.course_id, len(segs))
        return segs

    def stop_and_upload(self) -> None:
        """停止录制 → 完整性检查由 uploader 完成 → 上传 COS + 元数据。

        上传失败不退出进程：分段文件保留在本地 out_dir，记录 error 日志，
        主循环继续运行，待网络/配置恢复后可人工补传。
        """
        if self.current is None:
            return
        tag = self.current.tag
        course_id = self.current.course_id
        segs = self.stop_recording()
        if segs:
            try:
                self.uploader.upload_lesson(self.current, segs)
            except Exception:
                log.exception("上传失败，分段已保留在 %s，恢复后可补传 course=%s",
                              self.out_dir, course_id)
            else:
                self._reclaim_after_upload(segs, tag)
        self.proc, self.current = None, None

    # ---------- 本地回收（方案1：上传成功后按保留期清理本地分段） ----------
    def _reclaim_after_upload(self, segs: list[Path], tag: str) -> None:
        """上传成功后的本地回收：
        保留期 <= 0 → 立即删除；> 0 → 留到保留期，由 _sweep_local 周期回收。
        任何情况下都只删"已确认上传"的分段，绝不因清理丢录音。
        """
        try:
            days = float(self.cfg.get("local_retention_days", 7))
            if days <= 0:
                ok, fail = delete_group(segs)
                log.info("本地回收[立即] %s：删除 %d 段（失败 %d）", tag, ok, fail)
            else:
                log.info("本地保留 %s（保留期 %.0f 天，到期后周期回收）", tag, days)
        except Exception:  # noqa: BLE001 回收失败绝不影响主流程
            log.exception("本地回收异常（忽略） tag=%s", tag)

    def _sweep_local(self) -> None:
        """周期性回收：删除「已确认上传 COS 且超过保留期」的本地分段（默认 30 分钟一次）。

        判定与 local_cleanup 一致：COS 有 meta.json（整课就绪）才删；
        录制中的课程、未满保留期的、未确认上传的一律保留。
        """
        interval = int(self.cfg.get("local_cleanup_interval_sec", 1800))
        if time.monotonic() - self._last_sweep < interval:
            return
        self._last_sweep = time.monotonic()
        try:
            days = float(self.cfg.get("local_retention_days", 7))
            groups = group_local(self.out_dir)
            if not groups:
                return
            window = int(self.cfg.get("segment_seconds", 300)) + 120
            now = time.time()
            is_uploaded = make_uploaded_checker(
                self.uploader.client, self.uploader.bucket,
                self.cfg.get("org_id", "org"), self.cfg["room_id"])
            for tag, files in sorted(groups.items()):
                newest = max(f.stat().st_mtime for f in files)
                if now - newest < window:
                    continue                       # 录制中，绝不碰
                if (now - newest) / 86400.0 < days:
                    continue                       # 未到保留期
                if not is_uploaded(tag):
                    log.warning("本地残留未见 COS meta，保留待补传：%s", tag)
                    continue
                ok, fail = delete_group(files)
                log.info("本地回收[到期] %s：删除 %d 段（失败 %d）", tag, ok, fail)
        except Exception:  # noqa: BLE001
            log.exception("本地周期回收异常（忽略）")

    # ---------- 手动控制（监控面板按钮 → control.json → 此处消费） ----------
    def _poll_control(self, now: datetime) -> None:
        """消费手动控制指令：{"action":"start","course_id":..,"teacher":..} / {"action":"stop"}。"""
        if not _CONTROL_FILE.exists():
            return
        try:
            data = json.loads(_CONTROL_FILE.read_text(encoding="utf-8"))
            _CONTROL_FILE.unlink()
        except Exception:
            log.exception("读取手动控制指令失败")
            return
        action = data.get("action")
        if action == "start":
            if self.current is not None:
                log.warning("手动开始被忽略：正在录制 %s", self.current.course_id)
                return
            course_id = (data.get("course_id") or "").strip()
            if not course_id:
                course_id = "MANUAL-" + now.strftime("%Y%m%d_%H%M")
            # 清理 COS key / 文件名不安全的字符
            course_id = re.sub(r'[\\/:*?"<>|\s]+', "-", course_id)
            teacher = (data.get("teacher") or "").strip() or "未知"
            max_sec = int(data.get("max_sec") or self.cfg.get("manual_max_sec", 6 * 3600))
            lesson = Lesson(course_id=course_id, teacher=teacher, start=now,
                            end=now + timedelta(seconds=max_sec),
                            room=self.cfg["room_id"])
            log.info("手动触发开始 course=%s teacher=%s 安全上限=%ds",
                     course_id, teacher, max_sec)
            self.start_lesson(lesson)
        elif action == "stop":
            if self.current is None:
                log.warning("手动停止被忽略：当前没有在录制")
                return
            log.info("手动触发停止 course=%s", self.current.course_id)
            self.stop_and_upload()
        elif action == "reload_audio":
            # 控制台「麦克风」页保存后下发：换设备 / 改阈值，不重启进程
            self._apply_audio_config(data)
        elif action == "set_teacher":
            # 控制台「录课」页下拉栏选择授课教师：热生效 + 写回 config.json
            # （重启后仍生效）。云端课表匹配按 meta 里的教师归属课程。
            teacher = (data.get("teacher") or "").strip()
            old = (self.cfg.get("selected_teacher") or "").strip()
            self.cfg["selected_teacher"] = teacher
            try:
                cfg_path = Path(self.cfg.get("_config_path")
                                or (Path(__file__).resolve().parent / "config.json"))
                new = json.loads(cfg_path.read_text(encoding="utf-8"))
                new["selected_teacher"] = teacher
                tmp = cfg_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(new, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                tmp.replace(cfg_path)
                log.info("授课教师已切换：%s → %s（%s）",
                         old or "（未选）", teacher or "（未选）", cfg_path.name)
            except Exception:  # noqa: BLE001
                log.exception("selected_teacher 写回 config.json 失败"
                              "（本进程内已生效，重启后回落旧值）")

    # ---------- 采集设备热重载（控制台「麦克风」页） ----------
    def _apply_audio_config(self, data: dict) -> None:
        """换麦克风 / 改 VAD 阈值后，不重启进程地让新配置生效。

        为什么必须"重起采集流"而不是只改文件：VAD 是在启动时把设备名
        写进 ffmpeg 命令行的（见 vad.py → DshowPcmStream），改 config.json
        不会影响已经在跑的那一路。这里做的是：停旧流 → 合并新配置 →
        写回 config.json（重启后仍生效）→ 起新流。

        录制中一律拒绝：正在录的那一路 ffmpeg 早已用旧设备名启动，
        中途换会让「VAD 听 A 麦、录制录 B 麦」，静音停录判定失去意义。
        """
        if self._audio_busy:
            log.info("音频配置重载进行中，忽略重复请求")
            return
        self._audio_busy = True
        try:
            cfg_path = Path(self.cfg.get("_config_path")
                            or (Path(__file__).resolve().parent / "config.json"))
            if self.current is not None:
                self._audio_note = {
                    "ok": False, "at": datetime.now().isoformat(timespec="seconds"),
                    "msg": f"正在录制 {self.current.course_id}，拒绝切换麦克风"
                           "（本课结束后再点『保存并应用』）"}
                log.warning("拒绝热切换麦克风：正在录制 %s", self.current.course_id)
                return

            new = json.loads(cfg_path.read_text(encoding="utf-8"))
            # 控制台也可把目标值直接带在指令里（两条路径都支持）
            if data.get("device"):
                new["audio_device"] = data["device"]
            if data.get("voice_rms"):
                new["voice_rms"] = float(data["voice_rms"])

            old_dev = self.cfg.get("audio_device")
            old_rms = self.cfg.get("voice_rms")
            # 写回文件，保证重启后仍是新配置
            tmp = cfg_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(new, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(cfg_path)

            for key in ("audio_device", "audio_fmt", "voice_rms", "enable_vad",
                        "vad_buffer_sec", "segment_seconds", "sample_rate"):
                if key in new:
                    self.cfg[key] = new[key]

            # 先停旧采集流，避免同一时刻两路 VAD 抢同一设备
            if self.vad is not None:
                self.vad.stop()
                self.vad = None
            if self.cfg.get("enable_vad", False):
                vad = VadDetector(
                    audio_device=self.cfg["audio_device"],
                    audio_fmt=self.cfg.get("audio_fmt", "dshow"),
                    voice_rms=self.cfg.get("voice_rms", 0.02),
                    buffer_sec=float(self.cfg.get("vad_buffer_sec", 90)))
                vad.start()
                self.vad = vad

            self._audio_note = {
                "ok": True, "at": datetime.now().isoformat(timespec="seconds"),
                "msg": f"设备 {old_dev} → {self.cfg.get('audio_device')}；"
                       f"VAD 阈值 {old_rms} → {self.cfg.get('voice_rms')}",
            }
            log.info("音频配置已热重载：设备 %s → %s，voice_rms %s → %s（VAD %s）",
                     old_dev, self.cfg.get("audio_device"), old_rms,
                     self.cfg.get("voice_rms"),
                     "已重起" if self.vad else "未启用")
        except Exception as exc:  # noqa: BLE001
            self._audio_note = {
                "ok": False, "at": datetime.now().isoformat(timespec="seconds"),
                "msg": f"热重载失败：{type(exc).__name__}: {exc}"}
            log.exception("音频配置热重载失败")
        finally:
            self._audio_busy = False

    # ---------- 主循环 ----------
    def run_forever(self) -> None:
        vad: VadDetector | None = None
        if self.cfg.get("enable_vad", False):
            # buffer_sec 必须 >= identity.probe_sec，否则 snapshot_recent 取不满
            vad = VadDetector(audio_device=self.cfg["audio_device"],
                          audio_fmt=self.cfg.get("audio_fmt", "dshow"),
                          voice_rms=self.cfg.get("voice_rms", 0.02),
                          buffer_sec=float(self.cfg.get("vad_buffer_sec", 90)))
            vad.start()
        self.vad = vad
        log.info("录课终端启动 room=%s vad=%s", self.cfg["room_id"], bool(vad))

        while True:
            now = datetime.now()
            self.write_status()
            self._sweep_local()  # 周期回收本地已上传分段（内部限频）

            # 1. 停止判定
            if self.current is not None:
                if now >= self.current.end:
                    log.info("到点，停止录制")
                    self.stop_and_upload()
                elif (self.current.is_vad and vad is not None
                      and vad.silence_seconds() >= self.cfg.get("vad_stop_sec", 180.0)):
                    log.info("静音超时，停止录制")
                    self.stop_and_upload()

            # 1.5 手动控制指令（监控面板"开始/停止录制"按钮）
            self._poll_control(now)

            # 2. VAD 触发开始（仅待机时）
            if (self.current is None and vad is not None
                    and vad.voice_seconds() >= self.cfg.get("vad_start_sec", 3.0)):
                log.info("检测到持续人声，VAD 触发开始")
                self.start_vad_lesson(now)

            # 3. 课表优先：VAD 课进行中，若课表课即将开始，切换
            if self.current is not None and self.current.is_vad:
                pending = self._pending_lesson(now)
                if pending is not None:
                    log.info("课表课即将开始，切换录制")
                    self.stop_and_upload()

            # 4. 课表触发开始（仅待机时，开课前 3 分钟内）
            if self.current is None:
                pending = self._pending_lesson(now)
                if pending is not None:
                    log.info("课表触发开始 course=%s", pending.course_id)
                    self.start_lesson(pending)

            time.sleep(1)


def _acquire_singleton_lock(name: str):
    """单实例守护：锁定 status/<name>.lock 首字节，进程退出由 OS 自动释放。
    已有实例持锁时返回 None；成功时返回文件句柄（须存活至进程结束）。"""
    import msvcrt
    import os
    lock_path = _STATUS_DIR / f"{name}.lock"
    _STATUS_DIR.mkdir(parents=True, exist_ok=True)
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


def main() -> None:
    ap = argparse.ArgumentParser(description="录课终端")
    # 默认配置取脚本同目录，保证开机自启等任意工作目录下都能找到
    default_cfg = Path(__file__).resolve().parent / "config.json"
    ap.add_argument("--config", default=str(default_cfg), help="配置文件路径")
    args = ap.parse_args()
    cfg_path = Path(args.config)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    # schedule_file 相对路径一律相对配置文件所在目录解析
    sf = Path(cfg.get("schedule_file", "lessons.json"))
    if not sf.is_absolute():
        cfg["schedule_file"] = str((cfg_path.parent / sf).resolve())
    # 记住配置文件位置：控制台热切换麦克风时要写回同一个文件
    cfg["_config_path"] = str(cfg_path.resolve())
    _singleton = _acquire_singleton_lock("recorder")
    if _singleton is None:
        log.warning("已有录制服务实例在运行，本次启动退出")
        return
    Recorder(cfg).run_forever()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.exception("录课终端异常退出")
        raise
