# -*- coding: utf-8 -*-
"""课堂录制系统监控面板（PySide6 窗口程序）。

实时展示：
1. 服务状态：录制服务 / 云端流水线 是否在运行（基于状态文件心跳）
2. 录音进度：当前课程、已录时长、分段数、录音大小、进度条；今日课程表
3. 上传进度：字节级进度条、分段进度；本地残留分段一键补传
4. 教评报告：COS 报告列表、在线查看、下载到本地

数据来源：
- C:/recorder/status/recorder_status.json  （recorder.py 每秒写入）
- C:/recorder/status/upload_status.json    （uploader.py 上传时写入）
- C:/recorder/auto_pipeline_heartbeat.txt  （auto_pipeline.py 心跳）
- COS bucket: jiaopei/reports/**_report.txt

启动：pythonw monitor_gui.py（或双击 start_monitor.bat / 桌面快捷方式）
"""
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

RECORDER_DIR = Path(r"C:/recorder/recorder")
CLOUD_DIR = Path(r"C:/recorder/cloud")
STATUS_DIR = Path(r"C:/recorder/status")
CONTROL_FILE = STATUS_DIR / "control.json"  # 手动控制指令通道（recorder.py 消费）
PIPELINE_HEARTBEAT = Path(r"C:/recorder/auto_pipeline_heartbeat.txt")
REPORT_DIR = Path(r"C:/recorder/reports")

sys.path.insert(0, str(RECORDER_DIR))  # 复用 uploader.py

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)


def _load_json(path: Path) -> dict | None:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return None


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def _fmt_dur(sec: float) -> str:
    sec = max(0, int(sec))
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _parse_dt(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except Exception:  # noqa: BLE001
        return None


class CosWorker(QThread):
    """后台线程：列出 COS 报告 / 下载报告内容 / 下载报告文件。"""
    list_done = Signal(list, str)       # items, error
    text_done = Signal(str, str, str)   # key, text, error
    file_done = Signal(str, str, str)   # key, local_path, error

    # 报告文件识别规则：新链路（教评 txt/pdf + 专注力 html/xlsx）+ 旧命名
    _REPORT_SUFFIXES = ("_report.txt",)
    _REPORT_PREFIXES = ("教评报告_", "专注力报告_")
    _REPORT_EXTS = (".txt", ".pdf", ".html", ".xlsx")

    @classmethod
    def _is_report(cls, key: str) -> bool:
        name = key.rsplit("/", 1)[-1]
        if key.endswith(cls._REPORT_SUFFIXES):
            return True
        return name.startswith(cls._REPORT_PREFIXES) and name.endswith(cls._REPORT_EXTS)

    def __init__(self, cos_cfg: dict, prefix: str):
        super().__init__()
        self.cos_cfg = cos_cfg
        self.prefix = prefix
        self._action = "list"
        self._key = ""

    def list_reports(self):
        self._action = "list"
        self.start()

    def fetch_text(self, key: str):
        self._action = "text"
        self._key = key
        self.start()

    def fetch_file(self, key: str):
        """下载二进制报告（pdf/html/xlsx）到本地报告目录。"""
        self._action = "file"
        self._key = key
        self.start()

    def _client(self):
        from qcloud_cos import CosConfig, CosS3Client
        return CosS3Client(CosConfig(
            Region=self.cos_cfg["region"],
            SecretId=self.cos_cfg["secret_id"],
            SecretKey=self.cos_cfg["secret_key"],
        ))

    def run(self):
        try:
            client = self._client()
            if self._action == "list":
                items, marker = [], ""
                while True:
                    resp = client.list_objects(
                        Bucket=self.cos_cfg["bucket"], Prefix=self.prefix,
                        Marker=marker, MaxKeys=1000)
                    for c in resp.get("Contents", []):
                        if self._is_report(c["Key"]):
                            items.append({
                                "key": c["Key"],
                                "size": int(c["Size"]),
                                "mtime": c["LastModified"],
                            })
                    if resp.get("IsTruncated") == "true":
                        marker = resp.get("NextMarker", "")
                    else:
                        break
                items.sort(key=lambda x: x["key"], reverse=True)
                self.list_done.emit(items, "")
            elif self._action == "text":
                resp = client.get_object(
                    Bucket=self.cos_cfg["bucket"], Key=self._key)
                body = resp["Body"].get_raw_stream().read()
                self.text_done.emit(self._key, body.decode("utf-8", "replace"), "")
            else:  # file
                REPORT_DIR.mkdir(parents=True, exist_ok=True)
                local = REPORT_DIR / Path(self._key).name
                client.download_file(Bucket=self.cos_cfg["bucket"],
                                     Key=self._key, DestFilePath=str(local))
                self.file_done.emit(self._key, str(local), "")
        except Exception as e:  # noqa: BLE001
            if self._action == "list":
                self.list_done.emit([], str(e))
            elif self._action == "text":
                self.text_done.emit(self._key, "", str(e))
            else:
                self.file_done.emit(self._key, "", str(e))


class RetryWorker(QThread):
    """后台线程：补传本地残留分段（复用 uploader.Uploader，进度写入状态文件）。"""
    done = Signal(str, str)  # message, error

    def __init__(self, cfg: dict, groups: dict):
        super().__init__()
        self.cfg = cfg
        self.groups = groups  # tag -> [Path, ...]

    def run(self):
        try:
            from uploader import Uploader
            up = Uploader(self.cfg)
            total_ok = 0
            for tag, segs in sorted(self.groups.items()):
                parts = tag.rsplit("_", 2)  # course_id, YYYYMMDD, HHMMSS
                course_id = parts[0]
                start = datetime.strptime(parts[1] + "_" + parts[2], "%Y%m%d_%H%M%S")
                end = start.fromtimestamp(
                    max(s.stat().st_mtime for s in segs))
                lesson = SimpleNamespace(
                    course_id=course_id, teacher="补传", tag=tag,
                    start=start, end=end, room=self.cfg["room_id"])
                up.upload_lesson(lesson, segs)
                total_ok += len(segs)
                for s in segs:  # 上传成功后清理本地，否则残留列表永远不会清空
                    try:
                        s.unlink()
                    except OSError:
                        pass
            self.done.emit(f"补传完成，共上传 {total_ok} 个分段（{len(self.groups)} 节课）", "")
        except Exception:  # noqa: BLE001
            self.done.emit("", traceback.format_exc(limit=3))


class ReportDialog(QDialog):
    def __init__(self, parent, title: str, text: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(640, 560)
        v = QVBoxLayout(self)
        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setFont(QFont("Microsoft YaHei", 10))
        edit.setPlainText(text)
        v.addWidget(edit)
        btn = QPushButton("关闭")
        btn.clicked.connect(self.accept)
        v.addWidget(btn, alignment=Qt.AlignmentFlag.AlignRight)


class ManualStartDialog(QDialog):
    """手动开始录制：填写课程名称与老师。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("开始手动录制")
        self.setMinimumWidth(360)
        form = QFormLayout(self)
        self.ed_course = QLineEdit()
        self.ed_course.setPlaceholderText("例：数学-张三（留空自动命名）")
        self.ed_teacher = QLineEdit()
        self.ed_teacher.setPlaceholderText("例：张老师")
        form.addRow("课程名称", self.ed_course)
        form.addRow("授课老师", self.ed_teacher)
        tip = QLabel("录制将持续到点击「停止录制」为止；\n安全上限 6 小时自动停止并上传。")
        tip.setStyleSheet("color:#666;")
        form.addRow(tip)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def values(self) -> tuple[str, str]:
        course = self.ed_course.text().strip()
        teacher = self.ed_teacher.text().strip() or "未知"
        if not course:
            course = datetime.now().strftime("%Y%m%d_%H%M")
        if not course.startswith("MANUAL-"):
            course = "MANUAL-" + course
        return course, teacher


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("课堂录制监控面板")
        self.resize(720, 860)

        self.cfg = _load_json(RECORDER_DIR / "config.json") or {}
        cloud_cfg = _load_json(CLOUD_DIR / "cloud_config.json") or {}
        self.report_prefix = (
            cloud_cfg.get("report", {}).get("result_prefix") or "jiaopei/reports/")
        self.out_dir = Path(self.cfg.get("out_dir", r"C:/recordings"))
        sched = self.cfg.get("schedule_file", "lessons.json")
        self.schedule_file = (RECORDER_DIR / sched) if not Path(sched).is_absolute() else Path(sched)

        self.cos_worker: CosWorker | None = None
        self.retry_worker: RetryWorker | None = None
        self.reports: list[dict] = []
        # 手动控制：已发送但尚未在状态文件中看到生效的动作（start/stop）
        self._pending_action: str | None = None
        self._pending_since: datetime | None = None

        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)

        # ---------- 服务状态栏 ----------
        bar = QHBoxLayout()
        self.light_rec = self._make_light(bar, "录制服务")
        self.light_pipe = self._make_light(bar, "云端流水线")
        bar.addStretch(1)
        self.lbl_clock = QLabel("--")
        self.lbl_clock.setFont(QFont("Microsoft YaHei", 10))
        bar.addWidget(self.lbl_clock)
        v.addLayout(bar)

        # ---------- 录音 ----------
        gb_rec = QGroupBox("录音")
        form = QVBoxLayout(gb_rec)
        self.lbl_rec_state = QLabel("待机中")
        self.lbl_rec_state.setFont(QFont("Microsoft YaHei", 13, QFont.Weight.Bold))
        form.addWidget(self.lbl_rec_state)
        self.lbl_rec_detail = QLabel("当前没有课程在录制")
        self.lbl_rec_detail.setWordWrap(True)
        form.addWidget(self.lbl_rec_detail)
        self.bar_rec = QProgressBar()
        self.bar_rec.setRange(0, 1000)
        self.bar_rec.setValue(0)
        form.addWidget(self.bar_rec)
        # 手动控制按钮
        btn_row = QHBoxLayout()
        self.btn_man_start = QPushButton("▶ 开始手动录制")
        self.btn_man_start.setStyleSheet(
            "QPushButton{background:#2eb85c;color:white;padding:6px 14px;border-radius:4px;}"
            "QPushButton:disabled{background:#b9e2c8;}")
        self.btn_man_start.clicked.connect(self._manual_start)
        self.btn_man_stop = QPushButton("■ 停止录制")
        self.btn_man_stop.setStyleSheet(
            "QPushButton{background:#e55353;color:white;padding:6px 14px;border-radius:4px;}"
            "QPushButton:disabled{background:#eec2c2;}")
        self.btn_man_stop.clicked.connect(self._manual_stop)
        btn_row.addWidget(self.btn_man_start)
        btn_row.addWidget(self.btn_man_stop)
        btn_row.addStretch(1)
        form.addLayout(btn_row)
        self.lbl_next = QLabel("")
        self.lbl_next.setStyleSheet("color:#666;")
        self.lbl_next.setWordWrap(True)
        form.addWidget(self.lbl_next)
        v.addWidget(gb_rec)

        # ---------- 上传 ----------
        gb_up = QGroupBox("上传")
        uv = QVBoxLayout(gb_up)
        self.lbl_up_state = QLabel("无上传任务")
        self.lbl_up_state.setFont(QFont("Microsoft YaHei", 11, QFont.Weight.Bold))
        uv.addWidget(self.lbl_up_state)
        self.bar_up = QProgressBar()
        self.bar_up.setRange(0, 1000)
        uv.addWidget(self.bar_up)
        self.lbl_up_detail = QLabel("")
        self.lbl_up_detail.setWordWrap(True)
        uv.addWidget(self.lbl_up_detail)
        row = QHBoxLayout()
        self.lbl_leftover = QLabel("")
        row.addWidget(self.lbl_leftover, 1)
        self.btn_retry = QPushButton("一键补传")
        self.btn_retry.setEnabled(False)
        self.btn_retry.clicked.connect(self._retry_upload)
        row.addWidget(self.btn_retry)
        uv.addLayout(row)
        v.addWidget(gb_up)

        # ---------- 报告 ----------
        gb_rp = QGroupBox("教评 / 专注力报告（txt 双击在线查看，PDF/HTML/Excel 双击下载后打开）")
        rv = QVBoxLayout(gb_rp)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["课程 / 日期", "大小", "生成时间"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.itemDoubleClicked.connect(self._view_report)
        rv.addWidget(self.table)
        rrow = QHBoxLayout()
        self.lbl_rp_status = QLabel("")
        rrow.addWidget(self.lbl_rp_status, 1)
        self.btn_refresh = QPushButton("刷新报告列表")
        self.btn_refresh.clicked.connect(self.refresh_reports)
        rrow.addWidget(self.btn_refresh)
        self.btn_report_dir = QPushButton("打开本地报告目录")
        self.btn_report_dir.clicked.connect(self._open_report_dir)
        rrow.addWidget(self.btn_report_dir)
        rv.addLayout(rrow)
        v.addWidget(gb_rp)

        # 定时器：1s 刷新本地状态；60s 自动刷新报告列表
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._cos_tick = 0
        self._tick()
        QTimer.singleShot(500, self.refresh_reports)

    # ---------------- UI 辅助 ----------------
    def _make_light(self, layout, name: str) -> QLabel:
        lab = QLabel("●")
        lab.setFont(QFont("Microsoft YaHei", 12))
        txt = QLabel(name)
        txt.setFont(QFont("Microsoft YaHei", 10))
        layout.addWidget(lab)
        layout.addWidget(txt)
        layout.addSpacing(16)
        lab.setProperty("name", name)
        return lab

    @staticmethod
    def _set_light(lab: QLabel, ok: bool | None, tip: str):
        color = {True: "#2eb85c", False: "#e55353", None: "#999"}[ok]
        lab.setStyleSheet(f"color:{color};")
        lab.setToolTip(tip)

    # ---------------- 每秒刷新 ----------------
    def _tick(self):
        now = datetime.now()
        self.lbl_clock.setText(now.strftime("%Y-%m-%d %H:%M:%S"))
        self._cos_tick += 1
        if self._cos_tick >= 60:
            self._cos_tick = 0
            self.refresh_reports()

        # --- 录制服务心跳 ---
        rec = _load_json(STATUS_DIR / "recorder_status.json")
        rec_alive = False
        if rec:
            ts = _parse_dt(rec.get("updated_at", ""))
            rec_alive = bool(ts and (now - ts).total_seconds() <= 10)
        self._set_light(
            self.light_rec, rec_alive,
            "录制服务运行中" if rec_alive else
            "录制服务未运行（或未升级到带状态输出版本，请重启录制服务）")

        # --- 云端流水线心跳 ---
        pipe_alive = None
        if PIPELINE_HEARTBEAT.exists():
            age = time.time() - PIPELINE_HEARTBEAT.stat().st_mtime
            pipe_alive = age <= 180
            tip = f"云端流水线运行中（心跳 {int(age)} 秒前）" if pipe_alive \
                else f"云端流水线无心跳（已 {int(age // 60)} 分钟）"
        else:
            tip = "未找到流水线心跳文件（auto_pipeline.py 未运行）"
        self._set_light(self.light_pipe, pipe_alive, tip)

        self._update_recording(rec, rec_alive, now)
        self._update_upload(now)

    # ---------------- 手动控制 ----------------
    def _send_control(self, payload: dict) -> bool:
        try:
            STATUS_DIR.mkdir(parents=True, exist_ok=True)
            payload["ts"] = datetime.now().isoformat(timespec="seconds")
            tmp = CONTROL_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(CONTROL_FILE)
            return True
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "发送失败", f"无法写入控制指令：\n{e}")
            return False

    def _manual_start(self):
        dlg = ManualStartDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        course, teacher = dlg.values()
        if self._send_control({"action": "start", "course_id": course,
                               "teacher": teacher}):
            self._pending_action = "start"
            self._pending_since = datetime.now()
            self.lbl_rec_detail.setText(f"正在启动录制（{course}）…")

    def _manual_stop(self):
        course = ""
        rec = _load_json(STATUS_DIR / "recorder_status.json") or {}
        if rec.get("lesson"):
            course = rec["lesson"].get("course_id", "")
        if QMessageBox.question(
                self, "停止录制",
                f"确定停止当前录制{f'（{course}）' if course else ''}并上传吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        if self._send_control({"action": "stop"}):
            self._pending_action = "stop"
            self._pending_since = datetime.now()
            self.lbl_rec_detail.setText("正在停止并上传分段，请稍候…")

    def _update_recording(self, rec: dict | None, alive: bool, now: datetime):
        recording = bool(alive and rec and rec.get("state") == "recording"
                         and rec.get("lesson"))
        if recording:
            ls = rec["lesson"]
            start = _parse_dt(ls.get("start", ""))
            end = _parse_dt(ls.get("end", ""))
            elapsed = (now - start).total_seconds() if start else 0
            trigger = ls.get("trigger") or ("VAD 触发" if ls.get("is_vad") else "课表")
            stopping = self._pending_action == "stop"
            if self._pending_action == "start":
                self._pending_action = None  # start 已生效
            state_txt = (f"● 停止中：{ls.get('course_id', '?')}" if stopping
                         else f"● 录音中：{ls.get('course_id', '?')}")
            self.lbl_rec_state.setText(state_txt)
            self.lbl_rec_state.setStyleSheet("color:#e55353;")
            detail = (f"老师：{ls.get('teacher', '?')}　触发方式：{trigger}\n"
                      f"开始：{ls.get('start', '?')[11:19]}　已录：{_fmt_dur(elapsed)}　"
                      f"分段：{ls.get('segments', 0)} 个　大小：{_fmt_bytes(ls.get('bytes', 0))}")
            if stopping:
                detail += "\n正在停止并上传分段（进度见下方「上传」区）…"
            self.lbl_rec_detail.setText(detail)
            if trigger == "手动":
                # 手动录制无确定结束时间：忙碌动画
                self.bar_rec.setRange(0, 0)
            else:
                self.bar_rec.setRange(0, 1000)
                if start and end and end > start:
                    pct = min(1.0, elapsed / (end - start).total_seconds())
                    self.bar_rec.setValue(int(pct * 1000))
                    self.bar_rec.setFormat(f"{pct * 100:.0f}%（预计 {ls.get('end', '')[11:19]} 结束）")
                else:
                    self.bar_rec.setValue(0)
                    self.bar_rec.setFormat("")
        else:
            if self._pending_action == "stop":
                self._pending_action = None  # stop 已生效
            self.lbl_rec_state.setText("待机中")
            self.lbl_rec_state.setStyleSheet("color:#2eb85c;")
            if not alive:
                self._pending_action = None
                self.lbl_rec_detail.setText(
                    "录制服务未运行，无法录制。\n"
                    "请双击 C:\\recorder\\start_recorder.bat 启动（或重启电脑自动加载）。")
            elif self._pending_action == "start":
                wait = (now - (self._pending_since or now)).total_seconds()
                if wait <= 12:
                    self.lbl_rec_detail.setText("正在启动录制…（等待录制服务响应）")
                else:
                    self._pending_action = None
                    self.lbl_rec_detail.setText(
                        "启动命令未生效：录制服务可能未运行，请检查左上角服务灯。")
            else:
                self.lbl_rec_detail.setText(
                    "当前没有课程在录制。可点击下方「开始手动录制」随时开录。")
            self.bar_rec.setRange(0, 1000)
            self.bar_rec.setValue(0)
            self.bar_rec.setFormat("")

        # 按钮可用性：服务存活且状态匹配才可点
        self.btn_man_start.setEnabled(alive and not recording)
        self.btn_man_stop.setEnabled(alive and recording
                                     and self._pending_action != "stop")
        if not alive:
            self.btn_man_start.setToolTip("录制服务未运行")
            self.btn_man_stop.setToolTip("录制服务未运行")
        else:
            self.btn_man_start.setToolTip("")
            self.btn_man_stop.setToolTip("")

        # 今日课程表
        lessons = _load_json(self.schedule_file) or []
        today = now.date()
        lines = []
        for item in lessons:
            st = _parse_dt(item.get("start", ""))
            en = _parse_dt(item.get("end", ""))
            if st and st.date() == today:
                mark = "→ " if st <= now <= (en or st) else ("  " )
                lines.append(f"{mark}{st.strftime('%H:%M')}-{(en or st).strftime('%H:%M')} "
                             f"{item.get('course_id', '?')}（{item.get('teacher', '?')}）")
        self.lbl_next.setText("今日课表：" + ("　".join(lines) if lines else "无"))

    def _update_upload(self, now: datetime):
        up = _load_json(STATUS_DIR / "upload_status.json")
        state = (up or {}).get("state")
        if up and state == "uploading":
            course = up.get("course_id", "?")
            done, total = up.get("done_segments", 0), up.get("total_segments", 0)
            self.lbl_up_state.setText(f"▲ 正在上传：{course}")
            self.lbl_up_state.setStyleSheet("color:#339af0;")
            cur = up.get("current_segment") or {}
            seg_bytes = cur.get("bytes", 0)
            seg_up = cur.get("uploaded_bytes", 0)
            total_bytes = up.get("total_bytes", 0)
            done_bytes = seg_up  # 近似：已完分段字节 + 当前段已传
            # 已完分段字节无法精确，按分段比例估算整体进度
            pct = (done + (seg_up / seg_bytes if seg_bytes else 0)) / max(total, 1)
            self.bar_up.setValue(int(pct * 1000))
            self.bar_up.setFormat(
                f"{pct * 100:.0f}%　第 {cur.get('index', 0) + 1}/{total} 段")
            self.lbl_up_detail.setText(
                f"已完成分段 {done}/{total}　当前段 {_fmt_bytes(seg_up)}/{_fmt_bytes(seg_bytes)}"
                f"　总大小约 {_fmt_bytes(total_bytes)}")
        elif up and state == "done":
            self.lbl_up_state.setText(f"✓ 上传完成：{up.get('course_id', '?')}")
            self.lbl_up_state.setStyleSheet("color:#2eb85c;")
            self.bar_up.setValue(1000)
            self.bar_up.setFormat("100%")
            self.lbl_up_detail.setText(
                f"分段 {up.get('done_segments', 0)}/{up.get('total_segments', 0)}　"
                f"完成于 {up.get('finished_at', '?')[11:19]}")
        elif up and state == "failed":
            self.lbl_up_state.setText(f"✗ 上传失败：{up.get('course_id', '?')}")
            self.lbl_up_state.setStyleSheet("color:#e55353;")
            self.bar_up.setValue(0)
            self.lbl_up_detail.setText(f"原因：{up.get('error', '未知')[:200]}")
        else:
            self.lbl_up_state.setText("无上传任务")
            self.lbl_up_state.setStyleSheet("color:#999;")
            self.bar_up.setValue(0)
            self.bar_up.setFormat("")
            self.lbl_up_detail.setText("")

        # 本地残留分段
        groups = self._leftover_groups()
        n = sum(len(g) for g in groups.values())
        size = sum(s.stat().st_size for g in groups.values() for s in g)
        if n:
            self.lbl_leftover.setText(
                f"本地残留 {n} 个分段（{_fmt_bytes(size)}，{len(groups)} 节课）")
            self.lbl_leftover.setStyleSheet("color:#e58e3a;")
        else:
            self.lbl_leftover.setText("本地无残留分段")
            self.lbl_leftover.setStyleSheet("color:#999;")
        busy = state == "uploading" or (
            self.retry_worker is not None and self.retry_worker.isRunning())
        self.btn_retry.setEnabled(n > 0 and not busy)
        if self.retry_worker is not None and self.retry_worker.isRunning():
            self.btn_retry.setText("补传中…")
        else:
            self.btn_retry.setText("一键补传")

    def _leftover_groups(self) -> dict:
        """按课程 tag 分组本地 wav 分段（仅识别 课程_YYYYMMDD_HHMMSS_NNNNN.wav 命名）。

        仍在录制的课程（最新分段在 segment_seconds+120 秒内有写入）不算残留，
        避免补传拷走未写完的文件 / 误删导致课程结束时整课上传失败。
        """
        import re
        pat = re.compile(r"^(.+_\d{8}_\d{6})_\d{5}\.wav$")
        active_window = int(self.cfg.get("segment_seconds", 300)) + 120
        now = time.time()
        groups: dict[str, list[Path]] = {}
        try:
            for wav in self.out_dir.glob("*.wav"):
                m = pat.match(wav.name)
                if m:
                    groups.setdefault(m.group(1), []).append(wav)
        except Exception:  # noqa: BLE001
            pass
        return {tag: segs for tag, segs in groups.items()
                if now - max(s.stat().st_mtime for s in segs) >= active_window}

    # ---------------- 补传 ----------------
    def _retry_upload(self):
        groups = self._leftover_groups()
        if not groups:
            return
        n = sum(len(g) for g in groups.values())
        if QMessageBox.question(
                self, "确认补传",
                f"将把本地 {n} 个残留分段（{len(groups)} 节课）上传到 COS，继续？") \
                != QMessageBox.StandardButton.Yes:
            return
        self.retry_worker = RetryWorker(self.cfg, groups)
        self.retry_worker.done.connect(self._retry_done)
        self.retry_worker.start()

    def _retry_done(self, msg: str, err: str):
        if err:
            QMessageBox.warning(self, "补传失败", err)
        else:
            QMessageBox.information(self, "补传完成", msg)
        self._tick()

    # ---------------- 报告 ----------------
    def refresh_reports(self):
        cos_cfg = self.cfg.get("cos")
        if not cos_cfg:
            self.lbl_rp_status.setText("未找到 COS 配置")
            return
        if self.cos_worker is not None and self.cos_worker.isRunning():
            return
        self.lbl_rp_status.setText("正在加载…")
        self.cos_worker = CosWorker(cos_cfg, self.report_prefix)
        self.cos_worker.list_done.connect(self._reports_loaded)
        self.cos_worker.text_done.connect(self._report_text)
        self.cos_worker.file_done.connect(self._report_file)
        self.cos_worker.list_reports()

    def _reports_loaded(self, items: list, err: str):
        if err:
            self.lbl_rp_status.setText("加载失败：" + err[:120])
            return
        self.reports = items
        self.table.setRowCount(len(items))
        for row, it in enumerate(items):
            name = Path(it["key"]).name.replace("_report.txt", "").replace(".txt", "")
            self.table.setItem(row, 0, QTableWidgetItem(name))
            self.table.setItem(row, 1, QTableWidgetItem(_fmt_bytes(it["size"])))
            mtime = it["mtime"].replace("T", " ")[:19]
            self.table.setItem(row, 2, QTableWidgetItem(mtime))
        self.lbl_rp_status.setText(f"共 {len(items)} 份报告")

    def _view_report(self, item):
        row = item.row()
        if row >= len(self.reports):
            return
        key = self.reports[row]["key"]
        self.lbl_rp_status.setText("正在下载报告…")
        if self.cos_worker is None or not self.cos_worker.isRunning():
            self.cos_worker = CosWorker(self.cfg["cos"], self.report_prefix)
            self.cos_worker.list_done.connect(self._reports_loaded)
            self.cos_worker.text_done.connect(self._report_text)
            self.cos_worker.file_done.connect(self._report_file)
        if key.endswith(".txt"):
            self.cos_worker.fetch_text(key)
        else:  # pdf / html / xlsx：下载到本地后用系统默认程序打开
            self.cos_worker.fetch_file(key)

    def _report_file(self, key: str, local_path: str, err: str):
        if err:
            self.lbl_rp_status.setText("下载失败：" + err[:120])
            return
        self.lbl_rp_status.setText(f"已下载：{Path(local_path).name}")
        try:
            os.startfile(local_path)  # noqa: S606 调起系统默认程序查看
        except OSError as e:
            self.lbl_rp_status.setText("打开失败：" + str(e)[:120])

    def _open_report_dir(self):
        """打开本地报告目录（流水线产出：教评 txt/pdf + 专注力三件套）。"""
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(REPORT_DIR))  # noqa: S606
        except OSError as e:
            self.lbl_rp_status.setText("打开目录失败：" + str(e)[:120])

    def _report_text(self, key: str, text: str, err: str):
        if err:
            self.lbl_rp_status.setText("下载失败：" + err[:120])
            return
        self.lbl_rp_status.setText(f"共 {len(self.reports)} 份报告")
        # 同时保存一份到本地
        try:
            REPORT_DIR.mkdir(parents=True, exist_ok=True)
            (REPORT_DIR / Path(key).name).write_text(text, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        dlg = ReportDialog(self, Path(key).name, text)
        dlg.exec()


def _acquire_singleton_lock(name: str):
    """单实例守护：锁定 status/<name>.lock 首字节，进程退出由 OS 自动释放。
    已有实例持锁时返回 None；成功时返回文件句柄（须存活至进程结束）。"""
    import msvcrt
    fh = open(STATUS_DIR / f"{name}.lock", "a+b")
    fh.seek(0)
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return None
    return fh


def main():
    _singleton = _acquire_singleton_lock("monitor_gui")
    if _singleton is None:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, "监控面板已经在运行中，请查看任务栏。", "课堂录制监控", 0x40)
        return
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei", 9))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
