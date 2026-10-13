# -*- coding: utf-8 -*-
"""课堂录制系统 · 统一控制台（PySide6 单窗口）。

把原先散在三处的运维面收进一个前端：

    录课终端 recorder.py      录课 / VAD / 手动选定授课教师
    云端流水线 auto_pipeline.py  COS 巡检 → ASR → 角色判定 → 报告
    监控面板 monitor_gui.py     只读状态

控制台做四件原先没人做的事：
  1. **托管服务**：进程状态一眼可见，可一键拉起 / 重启；可选「自动守护」——
     发现服务掉线自动拉回来。2026-09-14 16:13 recorder 静默死亡、
     之后 23 小时无人察觉、当天 3 节课全丢，就是因为没人盯这个。
  2. **换麦克风**（「麦克风」页）：枚举 dshow 设备 → 实时电平 → 一键校准
     VAD 阈值 → 热重载生效（不重启进程、不断录制）。
  3. **选定授课教师**（「录课」页下拉栏）：课表归属不再依赖声纹识别，
     由人工选定教师，云端按该教师的飞书课表匹配课程。
  4. **飞书登录**（「飞书」页）：查看 OAuth 授权状态，一键登录 / 登出
     （课表同步依赖 lark-cli 的用户授权态）。

数据来源（全部是文件，前后端零耦合）：
  status/recorder_status.json      录制服务每秒写
  status/pipeline_status.json      云端流水线每轮写
  status/upload_status.json        上传进度
  status/control.json              下行指令（控制台写、recorder 消费）
  auto_pipeline_heartbeat.txt      流水线心跳
  status/<service>.lock            单实例锁，内含 " pid=NNNN"

启动：pythonw console.py（或双击 C:\\recorder\\启动课堂控制台.bat）
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(r"C:/recorder")
RECORDER_DIR = ROOT / "recorder"
CLOUD_DIR = ROOT / "cloud"
STATUS_DIR = ROOT / "status"
CONTROL_FILE = STATUS_DIR / "control.json"
LOG_DIR = ROOT / "logs"
REPORTS_DIR = ROOT / "reports"
GUARD_LOG = LOG_DIR / "console_guard.log"
CONSOLE_HEARTBEAT = ROOT / "console_heartbeat.txt"

for p in (str(ROOT), str(RECORDER_DIR), str(CLOUD_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import mic_devices  # noqa: E402
from PySide6.QtCore import Qt, QThread, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QColor, QFont, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSpinBox, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

# 复用已验证过的组件（COS 报告浏览 / 手动开录对话框）
from monitor_gui import CosWorker, ManualStartDialog, ReportDialog  # noqa: E402

import prompts as prompts_mod  # noqa: E402  提示词 / 报告模板注册表

GREEN, RED, GREY, BLUE, ORANGE = "#2eb85c", "#e55353", "#999999", "#339af0", "#e58e3a"

# ---------------------------------------------------------------- 小工具


def _load_json(path: Path) -> dict | None:
    try:
        if Path(path).exists():
            return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return None


def _fmt_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _fmt_dur(sec: float) -> str:
    sec = max(0, int(sec or 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _parse_dt(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except Exception:  # noqa: BLE001
        return None


def _pid_alive(pid: int) -> bool:
    if not pid or sys.platform != "win32":
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return code.value == 259  # STILL_ACTIVE
        return True
    finally:
        k32.CloseHandle(h)


def _light(lab: QLabel, state: str, tip: str) -> None:
    """state: ok / bad / warn / unknown"""
    color = {"ok": GREEN, "bad": RED, "warn": ORANGE, "unknown": GREY}[state]
    lab.setStyleSheet(f"color:{color};")
    lab.setToolTip(tip)


def _log_guard(msg: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with GUARD_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 服务托管

SERVICES: dict[str, dict] = {
    "recorder": {
        "title": "录课服务",
        "script": RECORDER_DIR / "recorder.py",
        "cwd": RECORDER_DIR,
        "lock": "recorder",
        "status": STATUS_DIR / "recorder_status.json",
        "fresh_sec": 15,
        "desc": "ffmpeg dshow 采集 + VAD 触发 + 手动选定授课教师",
    },
    "pipeline": {
        "title": "云端流水线",
        "script": CLOUD_DIR / "auto_pipeline.py",
        "cwd": CLOUD_DIR,
        "lock": "auto_pipeline",
        "heartbeat": ROOT / "auto_pipeline_heartbeat.txt",
        "fresh_sec": 300,
        "desc": "COS 巡检 → ASR 转写 → 角色判定 → 报告回传",
    },
}

_PYTHONW = ROOT / "venv" / "Scripts" / "pythonw.exe"


class ServiceManager:
    """读锁文件/心跳判断健康度，并能拉起 / 停止服务进程。"""

    def health(self, key: str) -> dict:
        svc = SERVICES[key]
        pid = self._pid(key)
        now = datetime.now()
        fresh, detail = False, ""

        if "status" in svc:
            st = _load_json(svc["status"]) or {}
            ts = _parse_dt(st.get("updated_at", ""))
            if ts:
                age = (now - ts).total_seconds()
                fresh = age <= svc["fresh_sec"]
                detail = (f"状态文件 {int(age)} 秒前更新"
                          if fresh else f"状态文件已停更 {_fmt_dur(age)}")
            else:
                detail = "无状态文件"
        else:
            hb = svc["heartbeat"]
            if hb.exists():
                age = time.time() - hb.stat().st_mtime
                fresh = age <= svc["fresh_sec"]
                detail = (f"心跳 {int(age)} 秒前"
                          if fresh else f"心跳已停 {_fmt_dur(age)}")
            else:
                detail = "无心跳文件"

        if fresh:
            state = "ok"
        elif pid and _pid_alive(pid):
            state = "warn"          # 进程在，但不干活（卡死）
            detail += "，但进程仍存活 → 疑似卡死"
        else:
            state = "bad"
            detail += "，进程已不在"

        return {"key": key, "title": svc["title"], "state": state,
                "pid": pid, "alive": state == "ok", "detail": detail,
                "running": bool(pid and _pid_alive(pid))}

    def _pid(self, key: str) -> int | None:
        """取服务 PID。

        优先读状态文件里的 pid 字段 —— *.lock 首字节被 msvcrt 独占锁定，
        进程活着的时候外部读它只会拿到 PermissionError（实测 9/15 复现）。
        仅当状态文件里没有时，才退回去读锁（此时进程多半已退出、锁已释放）。
        """
        svc = SERVICES[key]
        if "status" in svc:
            st = _load_json(svc["status"]) or {}
            pid = st.get("pid")
            if isinstance(pid, int) and pid > 0:
                return pid
        if svc.get("heartbeat"):
            st = _load_json(STATUS_DIR / "pipeline_status.json") or {}
            pid = st.get("pid")
            if isinstance(pid, int) and pid > 0:
                return pid
        lf = STATUS_DIR / f"{svc['lock']}.lock"
        try:
            txt = lf.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return None
        # 锁文件每次启动都在偏移 1 追加 pid，历史残留会留在后面 → 取最后一个
        hits = re.findall(r"pid=(\d+)", txt)
        return int(hits[-1]) if hits else None

    def start(self, key: str) -> tuple[bool, str]:
        svc = SERVICES[key]
        if not _PYTHONW.exists():
            return False, f"找不到 pythonw：{_PYTHONW}"
        try:
            subprocess.Popen(
                [str(_PYTHONW), str(svc["script"])],
                cwd=str(svc["cwd"]),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=(subprocess.DETACHED_PROCESS
                               | subprocess.CREATE_NEW_PROCESS_GROUP
                               | getattr(subprocess, "CREATE_NO_WINDOW", 0)))
            _log_guard(f"拉起 {svc['title']}（{svc['script'].name}）")
            return True, f"已发出启动：{svc['title']}"
        except Exception as exc:  # noqa: BLE001
            _log_guard(f"拉起 {svc['title']} 失败：{exc}")
            return False, f"启动失败：{exc}"

    def stop(self, key: str) -> tuple[bool, str]:
        pids = self._pids_thorough(key)
        if not pids:
            return True, f"{SERVICES[key]['title']} 本来就没在跑"
        killed, failed = [], []
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, text=True,
                               encoding="gbk", errors="ignore")
                killed.append(pid)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{pid}({exc})")
        _log_guard(f"停止 {SERVICES[key]['title']}：PIDs {killed}"
                   + (f" 失败 {failed}" if failed else ""))
        return True, f"已停止 {SERVICES[key]['title']}：PID {', '.join(map(str, killed))}"

    def _pids_thorough(self, key: str) -> list[int]:
        """找全某个服务的所有进程 PID。

        单靠锁文件不够：锁首字节被 msvcrt 独占，旧版服务运行期间它压根读不出来
        （PermissionError），控制台就会误判"没在跑"，结果新实例因单实例锁
        又立刻退出 —— 换了新代码却起不来。所以这里按命令行里的脚本名兜底。
        """
        pids: list[int] = []
        for pid in (self._pid(key),):
            if pid and _pid_alive(pid):
                pids.append(pid)
        script = SERVICES[key]["script"].name
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
                 "Where-Object { $_.CommandLine -like '*%s*' } | "
                 "Select-Object -ExpandProperty ProcessId" % script],
                capture_output=True, text=True, encoding="utf-8",
                errors="ignore", timeout=20,
                creationflags=(subprocess.CREATE_NO_WINDOW
                               if sys.platform == "win32" else 0)).stdout
            for tok in out.split():
                try:
                    pid = int(tok.strip())
                except ValueError:
                    continue
                if pid not in pids and _pid_alive(pid):
                    pids.append(pid)
        except Exception as exc:  # noqa: BLE001
            _log_guard(f"按脚本名查找 {script} 失败：{exc}")
        return pids

    def restart(self, key: str) -> tuple[bool, str]:
        self.stop(key)
        time.sleep(0.8)   # 等锁释放，否则新实例会因单实例锁直接退出
        return self.start(key)


# ---------------------------------------------------------------- 控件

class LevelBar(QWidget):
    """电平柱：画最近 N 个 RMS 块 + 一条阈值线（VAD 会不会触发一眼可见）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(46)
        self.values: list[float] = []
        self.threshold: float = 0.019
        self.note: str = ""

    def set_data(self, values: list[float], threshold: float, note: str = "") -> None:
        self.values = list(values or [])
        self.threshold = float(threshold or 0.02)
        self.note = note
        self.update()

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("#f7f7f7"))
        p.setPen(QPen(QColor("#dddddd")))
        p.drawRect(0, 0, w - 1, h - 1)
        if not self.values:
            p.setPen(QPen(QColor(GREY)))
            p.drawText(6, h // 2 + 5, "等待采集数据…")
            return
        # 纵轴按 4×阈值 归一（超出的截断），这样阈值线位置固定、好比较
        top = max(self.threshold * 4.0, 1e-6)
        n = len(self.values)
        bw = max(1.0, w / n)
        for i, v in enumerate(self.values):
            frac = min(1.0, v / top)
            bh = frac * (h - 4)
            color = QColor(GREEN) if v >= self.threshold else QColor("#9ec9e8")
            p.fillRect(int(i * bw), int(h - 2 - bh), max(1, int(bw - 1)), int(bh), color)
        y = int(h - 2 - (self.threshold / top) * (h - 4))
        p.setPen(QPen(QColor(RED), 2, Qt.PenStyle.DashLine))
        p.drawLine(0, y, w, y)
        p.setPen(QPen(QColor(RED)))
        p.drawText(w - 96, max(12, y - 3), f"阈值 {self.threshold:.4f}")


class ProbeWorker(QThread):
    """独立测一路麦克风的电平（阻塞 seconds 秒），用于「换麦前先试」。"""
    tick = Signal(float, float)
    done = Signal(dict)

    def __init__(self, device: str, seconds: float):
        super().__init__()
        self.device, self.seconds = device, seconds
        self._values: list[float] = []

    def run(self) -> None:
        res = mic_devices.test_device(
            self.device, self.seconds,
            on_tick=lambda el, v: (self._values.append(v), self.tick.emit(el, v)))
        res["values"] = self._values
        self.done.emit(res)


class CalibWorker(QThread):
    """两段式校准：先安静、后说话，算出建议 voice_rms。"""
    tick = Signal(float, float, str)
    phase = Signal(str)
    done = Signal(dict)

    def __init__(self, device: str, quiet: float, voice: float):
        super().__init__()
        self.device, self.quiet, self.voice = device, quiet, voice

    def run(self) -> None:
        res = mic_devices.calibrate(
            self.device, self.quiet, self.voice,
            on_phase=lambda ph: self.phase.emit(ph),
            on_tick=lambda el, v, ph: self.tick.emit(el, v, ph))
        self.done.emit(res)


class UploadedTagsWorker(QThread):
    """后台核验：COS 上哪些课程 tag 已经有 meta.json（= 已完整上传）。

    为什么需要它：本地分段是**有意保留 7 天**的（local_retention_days），
    所以「本地有文件」≠「残留待补传」。不核验就报"残留 98 个分段"，
    会诱导用户点「一键补传」把 ~940MB 已上传的音频重传一遍。
    """
    done = Signal(set, str)

    def __init__(self, cos_cfg: dict, org: str, room: str):
        super().__init__()
        self.cos_cfg, self.org, self.room = cos_cfg, org, room

    def run(self) -> None:
        try:
            if str(CLOUD_DIR) not in sys.path:
                sys.path.insert(0, str(CLOUD_DIR))
            from cos_client import CosStore
            store = CosStore(self.cos_cfg["bucket"], self.cos_cfg["region"],
                             self.cos_cfg["secret_id"], self.cos_cfg["secret_key"])
            entries = store.list_entries(f"{self.org}/{self.room}/")
            tags = set()
            for key in entries:
                name = key.rsplit("/", 1)[-1]
                if name.endswith("_meta.json"):
                    tags.add(name[:-len("_meta.json")])
            self.done.emit(tags, "")
        except Exception as exc:  # noqa: BLE001
            self.done.emit(set(), f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 页：总览

class OverviewPage(QWidget):
    def __init__(self, win: "Console"):
        super().__init__()
        self.win = win
        v = QVBoxLayout(self)

        gb = QGroupBox("服务托管")
        g = QGridLayout(gb)
        self.lights: dict[str, QLabel] = {}
        self.infos: dict[str, QLabel] = {}
        for row, key in enumerate(SERVICES):
            lab = QLabel("●"); lab.setFont(QFont("Microsoft YaHei", 14))
            title = QLabel(SERVICES[key]["title"])
            title.setFont(QFont("Microsoft YaHei", 11, QFont.Weight.Bold))
            info = QLabel("—"); info.setWordWrap(True)
            info.setStyleSheet("color:#555;")
            btns = QHBoxLayout()
            b_start = QPushButton("拉起"); b_start.clicked.connect(
                lambda _=False, k=key: self.win.start_service(k))
            b_restart = QPushButton("重启"); b_restart.clicked.connect(
                lambda _=False, k=key: self.win.restart_service(k))
            btns.addWidget(b_start); btns.addWidget(b_restart); btns.addStretch(1)
            g.addWidget(lab, row, 0)
            g.addWidget(title, row, 1)
            g.addWidget(info, row, 2)
            g.addLayout(btns, row, 3)
            self.lights[key], self.infos[key] = lab, info
        g.setColumnStretch(2, 3)

        row = QHBoxLayout()
        self.chk_guard = QCheckBox("自动守护（发现服务掉线自动拉回）")
        self.chk_guard.setChecked(True)
        self.chk_guard.setToolTip(
            "每 15 秒检查一次。录制服务靠状态文件停更判定掉线；\n"
            "重连续 2 次异常才动手，避免误拉。")
        row.addWidget(self.chk_guard)
        self.lbl_guard = QLabel("")
        self.lbl_guard.setStyleSheet("color:#777;")
        row.addWidget(self.lbl_guard, 1)
        b_all = QPushButton("全部拉起")
        b_all.clicked.connect(self.win.start_all)
        row.addWidget(b_all)
        g.addLayout(row, len(SERVICES), 0, 1, 4)
        v.addWidget(gb)

        gb2 = QGroupBox("今日课表")
        v2 = QVBoxLayout(gb2)
        self.tbl = QTableWidget(0, 4)
        self.tbl.setHorizontalHeaderLabels(["时间", "课程", "老师", "录制状态"])
        self.tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        v2.addWidget(self.tbl)
        v.addWidget(gb2, 1)

        gb3 = QGroupBox("告警")
        v3 = QVBoxLayout(gb3)
        self.alerts = QPlainTextEdit()
        self.alerts.setReadOnly(True)
        self.alerts.setMaximumHeight(120)
        v3.addWidget(self.alerts)
        v.addWidget(gb3)

    def refresh(self) -> None:
        for key in SERVICES:
            h = self.win.health.get(key) or {}
            _light(self.lights[key], h.get("state", "unknown"), h.get("detail", ""))
            pid = h.get("pid")
            self.infos[key].setText(
                f"{h.get('detail', '?')}　PID {pid or '—'}\n{SERVICES[key]['desc']}")
        g = self.win.guard_stats
        self.lbl_guard.setText(
            f"已自动拉起 {g['rescued']} 次｜最近：{g['last'] or '无'}")

        rec = self.win.rec_status or {}
        lessons = _load_json(RECORDER_DIR / "lessons.json") or []
        today = datetime.now().date()
        rows = []
        for it in lessons:
            st = _parse_dt(it.get("start", ""))
            if not st or st.date() != today:
                continue
            en = _parse_dt(it.get("end", ""))
            cur = rec.get("lesson") or {}
            if cur.get("course_id") == it.get("course_id"):
                mark = "● 录制中"
            elif en and en < datetime.now():
                mark = "已结束"
            elif st and st <= datetime.now() <= (en or st):
                mark = "⚠ 上课中但未录"
            else:
                mark = "待录"
            rows.append((f"{st:%H:%M}-{en:%H:%M}" if en else f"{st:%H:%M}",
                         it.get("course_id", ""), it.get("teacher", ""), mark))
        self.tbl.setRowCount(len(rows))
        for r, cells in enumerate(rows):
            for c, txt in enumerate(cells):
                item = QTableWidgetItem(txt)
                if c == 3:
                    if "录制中" in txt:
                        item.setForeground(QColor(GREEN))
                    elif "未录" in txt:
                        item.setForeground(QColor(RED))
                self.tbl.setItem(r, c, item)

        alerts = list(self.win.alerts)
        self.alerts.setPlainText("\n".join(f"• {a}" for a in alerts) if alerts
                                 else "（无告警）")


# ---------------------------------------------------------------- 页：录课

class RecordPage(QWidget):
    def __init__(self, win: "Console"):
        super().__init__()
        self.win = win
        v = QVBoxLayout(self)

        gb = QGroupBox("录音")
        f = QVBoxLayout(gb)
        self.lbl_state = QLabel("—")
        self.lbl_state.setFont(QFont("Microsoft YaHei", 14, QFont.Weight.Bold))
        f.addWidget(self.lbl_state)
        self.lbl_detail = QLabel("")
        self.lbl_detail.setWordWrap(True)
        f.addWidget(self.lbl_detail)
        self.bar = QProgressBar(); self.bar.setRange(0, 1000)
        f.addWidget(self.bar)
        row = QHBoxLayout()
        self.btn_start = QPushButton("▶ 开始手动录制")
        self.btn_start.setStyleSheet(
            "QPushButton{background:#2eb85c;color:white;padding:6px 14px;border-radius:4px;}"
            "QPushButton:disabled{background:#b9e2c8;}")
        self.btn_start.clicked.connect(self.win.manual_start)
        self.btn_stop = QPushButton("■ 停止并上传")
        self.btn_stop.setStyleSheet(
            "QPushButton{background:#e55353;color:white;padding:6px 14px;border-radius:4px;}"
            "QPushButton:disabled{background:#eec2c2;}")
        self.btn_stop.clicked.connect(self.win.manual_stop)
        row.addWidget(self.btn_start); row.addWidget(self.btn_stop); row.addStretch(1)
        f.addLayout(row)
        v.addWidget(gb)

        gb2 = QGroupBox("VAD 人声检测（决定 VAD 课能否自动开录）")
        f2 = QVBoxLayout(gb2)
        self.vad_state = QLabel("—"); f2.addWidget(self.vad_state)
        self.vad_bar = LevelBar(); f2.addWidget(self.vad_bar)
        self.vad_note = QLabel(""); self.vad_note.setWordWrap(True)
        self.vad_note.setStyleSheet("color:#666;")
        f2.addWidget(self.vad_note)
        v.addWidget(gb2)

        gb3 = QGroupBox("上传")
        f3 = QVBoxLayout(gb3)
        self.up_state = QLabel("无上传任务")
        self.up_state.setFont(QFont("Microsoft YaHei", 11, QFont.Weight.Bold))
        f3.addWidget(self.up_state)
        self.up_bar = QProgressBar(); self.up_bar.setRange(0, 1000)
        f3.addWidget(self.up_bar)
        self.up_detail = QLabel(""); self.up_detail.setWordWrap(True)
        f3.addWidget(self.up_detail)
        row3 = QHBoxLayout()
        self.leftover = QLabel(""); self.leftover.setWordWrap(True)
        row3.addWidget(self.leftover, 1)
        self.btn_verify = QPushButton("重新核验")
        self.btn_verify.setToolTip("向 COS 确认哪些课程已完整上传（区分"
                                   "「保留中」与「真残留」，避免误重传）")
        self.btn_verify.clicked.connect(self.win.check_uploaded)
        row3.addWidget(self.btn_verify)
        self.btn_retry = QPushButton("补传未上传")
        self.btn_retry.clicked.connect(self.win.retry_upload)
        row3.addWidget(self.btn_retry)
        f3.addLayout(row3)
        v.addWidget(gb3)

        gb4 = QGroupBox("授课教师（课表匹配依据）")
        f4 = QVBoxLayout(gb4)
        row4 = QHBoxLayout()
        row4.addWidget(QLabel("当前这间教室的授课老师："))
        self.cmb_teacher = QComboBox()
        self.cmb_teacher.setEditable(True)
        self.cmb_teacher.setMinimumWidth(200)
        self.cmb_teacher.setToolTip(
            "下拉选择或直接输入教师姓名（需与飞书课表「老师」列一致）。\n"
            "VAD / 手动触发的录制按此教师上报，云端按他/她的课表匹配课程；\n"
            "课表触发的录制沿用课表里的真实教师，不受此选择影响。")
        row4.addWidget(self.cmb_teacher, 1)
        self.btn_teacher = QPushButton("应用")
        self.btn_teacher.clicked.connect(self.apply_teacher)
        row4.addWidget(self.btn_teacher)
        f4.addLayout(row4)
        self.lbl_teacher = QLabel("—")
        self.lbl_teacher.setWordWrap(True)
        self.lbl_teacher.setStyleSheet("color:#666;")
        f4.addWidget(self.lbl_teacher)
        v.addWidget(gb4)
        v.addStretch(1)

    # -- 授课教师 ---------------------------------------------------------
    def reload_teachers(self) -> None:
        """填充教师候选：云配置教师名单 + 今日课表里出现的教师（去重保序）。"""
        current = (self.win.rec_status or {}).get("selected_teacher") \
            or self.win.selected_teacher or ""
        names = self.win.teacher_choices()
        self.cmb_teacher.blockSignals(True)
        self.cmb_teacher.clear()
        for n in names:
            self.cmb_teacher.addItem(n)
        if current:
            idx = self.cmb_teacher.findText(current)
            if idx >= 0:
                self.cmb_teacher.setCurrentIndex(idx)
            else:
                self.cmb_teacher.setCurrentText(current)
        self.cmb_teacher.blockSignals(False)

    def apply_teacher(self) -> None:
        name = self.cmb_teacher.currentText().strip()
        if not name:
            QMessageBox.warning(self, "未选择", "请先选择或输入授课教师姓名。")
            return
        self.win.set_selected_teacher(name)

    def refresh(self) -> None:
        rec = self.win.rec_status or {}
        alive = (self.win.health.get("recorder") or {}).get("alive", False)
        rec_lesson = rec.get("lesson") or {}
        recording = bool(alive and rec.get("state") == "recording" and rec_lesson)

        if recording:
            start = _parse_dt(rec_lesson.get("start", ""))
            end = _parse_dt(rec_lesson.get("end", ""))
            elapsed = (datetime.now() - start).total_seconds() if start else 0
            trigger = rec_lesson.get("trigger") or ("VAD 触发" if rec_lesson.get("is_vad") else "课表")
            self.lbl_state.setText(f"● 录音中：{rec_lesson.get('course_id', '?')}")
            self.lbl_state.setStyleSheet(f"color:{RED};")
            self.lbl_detail.setText(
                f"老师：{rec_lesson.get('teacher', '?')}　触发：{trigger}　"
                f"开始：{(rec_lesson.get('start') or '')[11:19]}\n"
                f"已录 {_fmt_dur(elapsed)}　分段 {rec_lesson.get('segments', 0)} 个　"
                f"大小 {_fmt_bytes(rec_lesson.get('bytes', 0))}　"
                f"预计 {(rec_lesson.get('end') or '')[11:19]} 结束")
            self.bar.setRange(0, 1000)
            if start and end and end > start:
                pct = min(1.0, elapsed / (end - start).total_seconds())
                self.bar.setValue(int(pct * 1000))
                self.bar.setFormat(f"{pct * 100:.0f}%")
            else:
                self.bar.setRange(0, 0)
        else:
            self.lbl_state.setText("待机中")
            self.lbl_state.setStyleSheet(f"color:{GREEN};")
            self.lbl_detail.setText("当前没有课程在录制。"
                                    + ("" if alive else "\n⚠ 录课服务未运行，先到「总览」页拉起。"))
            self.bar.setRange(0, 1000); self.bar.setValue(0); self.bar.setFormat("")

        self.btn_start.setEnabled(alive and not recording)
        self.btn_stop.setEnabled(alive and recording)

        # VAD
        audio = rec.get("audio") or {}
        lvl = audio.get("level") or {}
        thr = float(audio.get("voice_rms") or 0.02)
        if audio.get("vad_active"):
            self.vad_state.setText(
                f"运行中　设备：{audio.get('device_name', '?')}　阈值 {thr}")
            self.vad_state.setStyleSheet(f"color:{GREEN};")
            self.vad_bar.set_data(self.win.vad_series, thr)
            self.vad_note.setText(
                f"最近 10 秒：中位 {lvl.get('median', 0):.4f}／p95 {lvl.get('p95', 0):.4f}"
                f"／峰值 {lvl.get('peak', 0):.4f}　"
                f"（阈值 {thr}：柱超过虚线才判为人声）"
                + ("" if (lvl.get("p95") or 0) > thr else
                   "\n⚠ 电平明显低于阈值 —— VAD 课不会自动开录。"
                   "换麦克风后请到「麦克风」页一键校准。"))
        else:
            self.vad_state.setText("未运行（录课服务未启用 VAD 或未启动）")
            self.vad_state.setStyleSheet(f"color:{ORANGE};")
            self.vad_bar.set_data([], thr)

        # 上传
        up = self.win.upload_status or {}
        state = up.get("state")
        if state == "uploading":
            done, total = up.get("done_segments", 0), up.get("total_segments", 0)
            cur = up.get("current_segment") or {}
            sb = cur.get("bytes", 0) or 1
            pct = (done + (cur.get("uploaded_bytes", 0) / sb)) / max(total, 1)
            self.up_state.setText(f"▲ 正在上传：{up.get('course_id', '?')}")
            self.up_state.setStyleSheet(f"color:{BLUE};")
            self.up_bar.setValue(int(pct * 1000))
            self.up_bar.setFormat(f"{pct * 100:.0f}%　第 {cur.get('index', 0) + 1}/{total} 段")
            self.up_detail.setText(
                f"已完成 {done}/{total} 段　当前段 "
                f"{_fmt_bytes(cur.get('uploaded_bytes', 0))}/{_fmt_bytes(cur.get('bytes', 0))}")
        elif state == "done":
            self.up_state.setText(f"✓ 上传完成：{up.get('course_id', '?')}")
            self.up_state.setStyleSheet(f"color:{GREEN};")
            self.up_bar.setValue(1000); self.up_bar.setFormat("100%")
            self.up_detail.setText(
                f"分段 {up.get('done_segments', 0)}/{up.get('total_segments', 0)}　"
                f"完成于 {(up.get('finished_at') or '?')[11:19]}")
        elif state == "failed":
            self.up_state.setText(f"✗ 上传失败：{up.get('course_id', '?')}")
            self.up_state.setStyleSheet(f"color:{RED};")
            self.up_detail.setText(f"原因：{(up.get('error') or '未知')[:200]}")
        else:
            self.up_state.setText("无上传任务")
            self.up_state.setStyleSheet(f"color:{GREY};")
            self.up_bar.setValue(0); self.up_bar.setFormat("")
            self.up_detail.setText("")

        summ = self.win.local_summary()
        if summ["known"]:
            parts = []
            if summ["retained_n"]:
                parts.append(f"本地保留 {summ['retained_n']} 段"
                             f"（{_fmt_bytes(summ['retained_bytes'])}，"
                             f"已上传，{self.win.cfg.get('local_retention_days', 7)} 天后自动回收）")
            if summ["pending_n"]:
                parts.append(f"⚠ 未上传 {summ['pending_n']} 段"
                             f"（{_fmt_bytes(summ['pending_bytes'])}，{len(summ['pending'])} 节课）")
            self.leftover.setText("　".join(parts) if parts else "本地无分段文件")
            self.leftover.setStyleSheet(f"color:{ORANGE};" if summ["pending_n"]
                                        else f"color:{GREY};")
        else:
            self.leftover.setText(
                f"无法核验本地分段（{summ.get('error') or '尚未核验'}）"
                "　→ 已禁用补传，避免重传已上传的音频")
            self.leftover.setStyleSheet(f"color:{RED};")
        busy = state == "uploading" or self.win.retry_busy
        self.btn_retry.setEnabled(bool(summ["pending_n"]) and summ["known"] and not busy)
        self.btn_retry.setText("补传中…" if self.win.retry_busy else "补传未上传")
        self.btn_verify.setEnabled(not busy)

        # 授课教师（下拉候选每次刷新时轻量补齐；不打断正在输入的文本）
        self.reload_teachers()
        sel = self.win.selected_teacher
        rec_sel = (rec.get("selected_teacher") or "").strip()
        if rec_sel:
            self.lbl_teacher.setText(
                f"✅ 录课服务当前使用：{rec_sel}"
                + ("" if rec_sel == (sel or rec_sel) else
                   f"（下拉栏为 {sel}，点「应用」同步）"))
            self.lbl_teacher.setStyleSheet(f"color:{GREEN};")
        else:
            self.lbl_teacher.setText(
                "⚠ 尚未选定授课教师 —— VAD / 手动触发的录制会以「未知」上报，"
                "云端无法匹配课表，将仅存档不出报告。")
            self.lbl_teacher.setStyleSheet(f"color:{ORANGE};")


# ---------------------------------------------------------------- 页：麦克风

class AudioPage(QWidget):
    def __init__(self, win: "Console"):
        super().__init__()
        self.win = win
        self.devices: list[dict] = []
        self.worker: ProbeWorker | None = None
        self.calib: CalibWorker | None = None
        v = QVBoxLayout(self)

        gb = QGroupBox("当前生效的设备（录课服务正在用的）")
        f = QVBoxLayout(gb)
        self.cur = QLabel("—"); self.cur.setWordWrap(True)
        self.cur.setFont(QFont("Microsoft YaHei", 11, QFont.Weight.Bold))
        f.addWidget(self.cur)
        self.cur_bar = LevelBar(); f.addWidget(self.cur_bar)
        self.cur_note = QLabel(""); self.cur_note.setWordWrap(True)
        self.cur_note.setStyleSheet("color:#666;")
        f.addWidget(self.cur_note)
        v.addWidget(gb)

        gb2 = QGroupBox("更换麦克风")
        g = QGridLayout(gb2)
        g.addWidget(QLabel("设备："), 0, 0)
        self.combo = QComboBox(); self.combo.setMinimumWidth(380)
        g.addWidget(self.combo, 0, 1, 1, 2)
        self.btn_reload = QPushButton("刷新设备列表")
        self.btn_reload.clicked.connect(self.reload_devices)
        g.addWidget(self.btn_reload, 0, 3)
        g.addWidget(QLabel("VAD 阈值 voice_rms："), 1, 0)
        self.spin = QDoubleSpinBox()
        self.spin.setDecimals(5); self.spin.setRange(0.0001, 1.0)
        self.spin.setSingleStep(0.001)
        g.addWidget(self.spin, 1, 1)
        self.lbl_suggest = QLabel("")
        self.lbl_suggest.setStyleSheet("color:#777;")
        g.addWidget(self.lbl_suggest, 1, 2, 1, 2)

        row = QHBoxLayout()
        self.btn_probe = QPushButton("电平测试（10 秒）")
        self.btn_probe.clicked.connect(self.probe)
        self.btn_calib = QPushButton("一键校准（安静 15s + 说话 15s）")
        self.btn_calib.clicked.connect(self.calibrate)
        self.btn_apply = QPushButton("保存并应用到录课服务")
        self.btn_apply.setStyleSheet(
            "QPushButton{background:#339af0;color:white;padding:6px 14px;border-radius:4px;}")
        self.btn_apply.clicked.connect(self.apply)
        row.addWidget(self.btn_probe); row.addWidget(self.btn_calib)
        row.addWidget(self.btn_apply); row.addStretch(1)
        g.addLayout(row, 2, 0, 1, 4)

        self.probe_bar = LevelBar(); self.probe_bar.setMinimumHeight(60)
        g.addWidget(self.probe_bar, 3, 0, 1, 4)
        self.probe_note = QLabel("点「电平测试」后对着麦克风正常说话，看柱子能否稳定超过虚线。")
        self.probe_note.setWordWrap(True); self.probe_note.setStyleSheet("color:#666;")
        g.addWidget(self.probe_note, 4, 0, 1, 4)
        v.addWidget(gb2)

        gb3 = QGroupBox("为什么要重校阈值")
        f3 = QVBoxLayout(gb3)
        tip = QLabel(
            "不同麦克风的增益差 3~10 倍。沿用旧阈值会出现两种坏结果：\n"
            "　• 阈值偏高 → VAD「永远听不见」→ VAD 课根本不会自动开录\n"
            "　• 阈值偏低 → 环境噪声也算人声 → 从早录到晚、白烧 ASR 费用\n\n"
            "所以换麦的正确顺序是：插上新麦 → 刷新设备列表 → 选中它 → "
            "电平测试确认有声 → 一键校准 → 保存并应用。\n"
            "「保存并应用」是热重载：不重启进程、不断录制；但若正在录制会拒绝执行，"
            "本课结束后再点一次即可。")
        tip.setWordWrap(True); tip.setStyleSheet("color:#555;")
        f3.addWidget(tip)
        v.addWidget(gb3)
        v.addStretch(1)
        self.reload_devices()

    # -- 设备列表 ---------------------------------------------------------
    def reload_devices(self) -> None:
        self.devices = mic_devices.list_input_devices("audio")
        self.combo.clear()
        for d in self.devices:
            self.combo.addItem(d["name"])
        cfg = mic_devices.load_config()
        cur = mic_devices.strip_dshow_arg(cfg.get("audio_device", ""))
        idx = self.combo.findText(cur)
        if idx >= 0:
            self.combo.setCurrentIndex(idx)
        self.spin.setValue(float(cfg.get("voice_rms") or 0.02))
        self.lbl_suggest.setText(
            f"发现 {len(self.devices)} 个音频输入设备"
            + ("" if len(self.devices) != 1 else "（插上 USB 麦克风后点「刷新设备列表」）"))

    def current_device(self) -> str:
        return self.combo.currentText().strip()

    # -- 动作 -------------------------------------------------------------
    def probe(self) -> None:
        dev = self.current_device()
        if not dev:
            QMessageBox.warning(self, "没有设备", "先刷新设备列表并选中一个麦克风。")
            return
        if self.worker and self.worker.isRunning():
            return
        self._probe_values = []
        self.probe_note.setText(f"正在测「{dev}」10 秒，请正常说话…")
        self.worker = ProbeWorker(dev, 10.0)
        self.worker.tick.connect(self._probe_tick)
        self.worker.done.connect(self._probe_done)
        self.worker.start()

    def _probe_tick(self, _el: float, _v: float) -> None:
        self._probe_values.append(_v)
        self.probe_bar.set_data(self._probe_values[-200:], self.spin.value())

    def _probe_done(self, res: dict) -> None:
        self.probe_bar.set_data(res.get("values", [])[-200:], self.spin.value())
        self.probe_note.setText(
            f"【{res.get('verdict')}】{res.get('advice')}\n"
            f"中位 {res.get('median', 0):.4f}　p95 {res.get('p95', 0):.4f}　"
            f"峰值 {res.get('peak', 0):.4f}")

    def calibrate(self) -> None:
        dev = self.current_device()
        if not dev:
            QMessageBox.warning(self, "没有设备", "先刷新设备列表并选中一个麦克风。")
            return
        if self.calib and self.calib.isRunning():
            return
        if QMessageBox.question(
                self, "开始校准",
                "校准分两段：\n　前 15 秒 —— 保持安静（测噪声底）\n"
                "　后 15 秒 —— 正常说话（测人声电平）\n\n"
                "与上课时一样的音量、一样的距离。开始？") \
                != QMessageBox.StandardButton.Yes:
            return
        self.btn_calib.setEnabled(False)
        self.calib = CalibWorker(dev, 15.0, 15.0)
        self.calib.phase.connect(
            lambda ph: self.probe_note.setText(
                ">>> 请保持安静（测噪声底）<<<" if ph == "quiet"
                else ">>> 现在请正常说话 <<<"))
        self.calib.tick.connect(
            lambda el, v, _ph: self.probe_bar.set_data(
                [v] * min(200, int(el / 0.1)), self.spin.value()))
        self.calib.done.connect(self._calib_done)
        self.calib.start()

    def _calib_done(self, res: dict) -> None:
        self.btn_calib.setEnabled(True)
        if res.get("error"):
            self.probe_note.setText(f"校准失败：{res['error']}")
            return
        self.spin.setValue(float(res["suggest"]))
        self.lbl_suggest.setText(f"建议 {res['suggest']}（已填入左侧）")
        self.probe_note.setText(
            f"校准完成：静音中位 {res['quiet_median']:.4f}／p95 {res['quiet_p95']:.4f}　"
            f"人声 p05 {res['voice_p05']:.4f}／中位 {res['voice_median']:.4f}\n"
            f"→ 建议 voice_rms = {res['suggest']}"
            + ("" if res.get("separation_ok")
               else "\n⚠ 人声与静音区分度不足：环境太吵或麦克风增益太低，"
                    "建议改善后重测（硬用会导致 VAD 频繁误触发）"))

    def apply(self) -> None:
        dev = self.current_device()
        if not dev:
            return
        rms = float(self.spin.value())
        old = mic_devices.load_config()
        if QMessageBox.question(
                self, "保存并应用",
                f"设备：{old.get('audio_device')}\n　　→ {mic_devices.dshow_arg(dev)}\n"
                f"VAD 阈值：{old.get('voice_rms')} → {rms}\n\n"
                "将写入 config.json 并让录课服务热重载采集流。继续？") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            mic_devices.save_audio_config(dev, rms)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "写入失败", str(exc))
            return
        self.win.send_control({"action": "reload_audio",
                               "device": mic_devices.dshow_arg(dev),
                               "voice_rms": rms})
        self.probe_note.setText(
            "已下发热重载指令，2 秒后看「当前生效的设备」是否更新。"
            "若提示拒绝，说明正在录制 —— 本课结束后再点一次。")

    def refresh(self) -> None:
        audio = (self.win.rec_status or {}).get("audio") or {}
        lvl = audio.get("level") or {}
        thr = float(audio.get("voice_rms") or 0.02)
        if audio:
            self.cur.setText(
                f"{audio.get('device_name', '?')}　"
                f"（{audio.get('fmt', '?')}，阈值 {thr}，VAD "
                f"{'运行中' if audio.get('vad_active') else '未运行'}）")
            self.cur_bar.set_data(self.win.vad_series, thr)
            med = lvl.get("median") or 0
            verdict = ("无明显信号" if med < 0.0005 else
                       "偏低" if med < 0.003 else "正常" if med < 0.05 else "偏高")
            self.cur_note.setText(
                f"实时电平（复用录课服务采集，零额外占用）：中位 {med:.4f}／"
                f"p95 {lvl.get('p95', 0):.4f} → {verdict}"
                + (f"\n上次操作：{audio['note'].get('msg')}" if audio.get("note") else ""))
        else:
            self.cur.setText("录课服务未运行，读不到采集状态")
            self.cur_bar.set_data([], thr)


# ---------------------------------------------------------------- 页：飞书

def _lark_paths() -> tuple[str, str]:
    """解析 lark-cli 运行入口（复用 cloud/schedule_feishu 的定位逻辑：
    安装包 → vendor/node；开发机 → .workbuddy 托管目录）。"""
    try:
        if str(CLOUD_DIR) not in sys.path:
            sys.path.insert(0, str(CLOUD_DIR))
        from schedule_feishu import NODE_PATH_RESOLVED, LARK_CLI_RESOLVED
        return NODE_PATH_RESOLVED, LARK_CLI_RESOLVED
    except Exception:  # noqa: BLE001
        return "", ""


def run_lark(args: list[str], timeout: int = 30) -> dict:
    """跑一条 lark-cli 子命令，返回 {ok, data|raw|error}。不抛异常。"""
    node, lark = _lark_paths()
    if not node or not lark or not Path(node).exists() or not Path(lark).exists():
        return {"ok": False, "error": f"lark-cli 不可用：node={node or '未找到'}，"
                                      f"lark={lark or '未找到'}"}
    try:
        r = subprocess.run([node, lark, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=(subprocess.CREATE_NO_WINDOW
                                          if os.name == "nt" else 0))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"命令超时（>{timeout}s）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or out
                                       or f"退出码 {r.returncode}")[:300]}
    try:
        return {"ok": True, "data": json.loads(out)}
    except ValueError:
        return {"ok": True, "raw": out}


class LarkCmdWorker(QThread):
    """后台执行一条 lark-cli 命令（状态查询 / 登出等短命令）。"""
    done = Signal(dict)

    def __init__(self, args: list[str], timeout: int = 30):
        super().__init__()
        self.args, self.timeout = args, timeout

    def run(self) -> None:
        self.done.emit(run_lark(self.args, self.timeout))


class LarkLoginStartWorker(QThread):
    """设备流登录第一步：发起授权、立即拿验证 URL（不等待）。"""
    done = Signal(dict)

    def run(self) -> None:
        self.done.emit(run_lark(["auth", "login", "--no-wait", "--json"], 60))


class LarkLoginPollWorker(QThread):
    """设备流登录第二步：用 device_code 轮询，直到用户在浏览器完成授权。

    设备码有效期 10 分钟；超时/失败都以 done 上报，由界面提示重试。
    """
    done = Signal(dict)

    def __init__(self, device_code: str):
        super().__init__()
        self.device_code = device_code

    def run(self) -> None:
        self.done.emit(run_lark(
            ["auth", "login", "--device-code", self.device_code], 700))


class FeishuPage(QWidget):
    """飞书 OAuth 授权管理：课表同步依赖 lark-cli 的用户登录态。

    登录走 Device Flow：点「登录」→ 拿到验证 URL → 自动打开浏览器 →
    用户在飞书页面确认 → 后台轮询完成授权。全程无需手动复制粘贴。
    """

    def __init__(self, win: "Console"):
        super().__init__()
        self.win = win
        self._status_worker: LarkCmdWorker | None = None
        self._start_worker: LarkLoginStartWorker | None = None
        self._poll_worker: LarkLoginPollWorker | None = None
        self._logout_worker: LarkCmdWorker | None = None
        self._tick = 0

        v = QVBoxLayout(self)

        gb = QGroupBox("授权状态")
        f = QVBoxLayout(gb)
        self.sts = QLabel("查询中…")
        self.sts.setWordWrap(True)
        self.sts.setFont(QFont("Microsoft YaHei", 11))
        f.addWidget(self.sts)
        row = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新状态")
        self.btn_refresh.clicked.connect(self.refresh_status)
        row.addWidget(self.btn_refresh)
        row.addStretch(1)
        f.addLayout(row)
        v.addWidget(gb)

        gb2 = QGroupBox("登录（飞书 OAuth · 设备流）")
        f2 = QVBoxLayout(gb2)
        row2 = QHBoxLayout()
        self.btn_login = QPushButton("登录飞书")
        self.btn_login.setStyleSheet(
            "QPushButton{background:#339af0;color:white;padding:6px 14px;border-radius:4px;}")
        self.btn_login.clicked.connect(self.start_login)
        row2.addWidget(self.btn_login)
        self.btn_logout = QPushButton("登出")
        self.btn_logout.clicked.connect(self.logout)
        row2.addWidget(self.btn_logout)
        row2.addStretch(1)
        f2.addLayout(row2)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(180)
        self.log.setPlaceholderText("登录过程信息会显示在这里…")
        f2.addWidget(self.log)
        v.addWidget(gb2)

        gb3 = QGroupBox("说明")
        f3 = QVBoxLayout(gb3)
        tip = QLabel(
            "什么情况需要登录：\n"
            "　• 新机器安装后首次使用（授权态存在 %USERPROFILE%\\.lark-cli，无法跨机复制）\n"
            "　• 「云端」页课表同步一直 ❌、或总览课表为空\n\n"
            "登录流程：点「登录飞书」→ 浏览器自动打开飞书授权页 → 扫码/确认 →\n"
            "本页自动显示已登录。授权默认 7 天有效，过期后重新登录一次即可。\n"
            "课表同步由云端流水线执行（约 10 分钟一次），登录后等一两轮即可看到课表。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#555;")
        f3.addWidget(tip)
        v.addWidget(gb3)
        v.addStretch(1)

        QTimer.singleShot(800, self.refresh_status)

    # ---------------- 状态 ----------------
    def _say(self, text: str, color: str = GREY) -> None:
        self.sts.setText(text)
        self.sts.setStyleSheet(f"color:{color};")

    def refresh_status(self) -> None:
        if self._status_worker is not None and self._status_worker.isRunning():
            return
        self._status_worker = LarkCmdWorker(["auth", "status"], 30)
        self._status_worker.done.connect(self._status_done)
        self._status_worker.start()

    def _status_done(self, res: dict) -> None:
        if not res.get("ok"):
            self._say(f"❌ 无法查询登录状态：{res.get('error', '?')}", RED)
            return
        data = res.get("data") or {}
        user = (data.get("identities") or {}).get("user") or {}
        name = user.get("userName") or ""
        if user.get("status") == "ready" and user.get("available", True):
            exp = str(user.get("expiresAt") or "").replace("T", " ")[:16]
            self._say(f"✅ 已登录：{name or '（飞书用户）'}"
                      + (f"　访问令牌有效期至 {exp}" if exp else ""), GREEN)
        else:
            msg = user.get("message") or "未登录"
            self._say(f"⚠ 未登录（{msg}）—— 点下方「登录飞书」，"
                      "否则课表同步无法工作。", ORANGE)

    # ---------------- 登录 / 登出 ----------------
    def _append(self, text: str) -> None:
        self.log.appendPlainText(text)

    def start_login(self) -> None:
        if self._poll_worker is not None and self._poll_worker.isRunning():
            QMessageBox.information(self, "登录进行中",
                                    "上一轮登录还在等待你在浏览器里确认。")
            return
        if self._start_worker is not None and self._start_worker.isRunning():
            return
        self.btn_login.setEnabled(False)
        self._append(f"[{datetime.now():%H:%M:%S}] 正在发起授权…")
        self._start_worker = LarkLoginStartWorker()
        self._start_worker.done.connect(self._login_started)
        self._start_worker.start()

    def _login_started(self, res: dict) -> None:
        self.btn_login.setEnabled(True)
        if not res.get("ok"):
            self._append(f"发起授权失败：{res.get('error', '?')}")
            QMessageBox.warning(self, "登录失败", str(res.get("error")))
            return
        data = res.get("data") or {}
        url = data.get("verification_url") or ""
        code = data.get("device_code") or ""
        if not url or not code:
            self._append(f"授权响应不完整：{str(data)[:200]}")
            QMessageBox.warning(self, "登录失败", "lark-cli 未返回验证链接，详见日志。")
            return
        expires = int(data.get("expires_in") or 600)
        self._append(f"验证链接（{expires // 60} 分钟内有效）：\n{url}")
        self._append("已尝试自动打开浏览器；若没弹出，请手动复制上面的链接。")
        try:
            os.startfile(url)  # noqa: S606
        except Exception:  # noqa: BLE001
            pass
        self.btn_login.setEnabled(False)
        self.btn_login.setText("等待授权确认…")
        self._poll_worker = LarkLoginPollWorker(code)
        self._poll_worker.done.connect(self._login_done)
        self._poll_worker.start()

    def _login_done(self, res: dict) -> None:
        self.btn_login.setEnabled(True)
        self.btn_login.setText("登录飞书")
        self._poll_worker = None
        if not res.get("ok"):
            self._append(f"登录未完成：{res.get('error', '?')}")
            QMessageBox.warning(self, "登录未完成",
                                f"{res.get('error')}\n\n请点「登录飞书」重试。")
            return
        data = res.get("data") or {}
        ident = (data.get("identities") or {}).get("user") or {}
        name = ident.get("userName") or data.get("userName") or ""
        self._append(f"[{datetime.now():%H:%M:%S}] ✅ 登录成功：{name or '飞书用户'}")
        QMessageBox.information(self, "登录成功",
                                f"已登录：{name or '飞书用户'}\n"
                                "课表同步会在下一轮自动恢复（约 10 分钟内）。")
        self.refresh_status()

    def logout(self) -> None:
        if QMessageBox.question(
                self, "登出飞书",
                "确定登出当前飞书账号？\n登出后课表同步立即停止，直到重新登录。") \
                != QMessageBox.StandardButton.Yes:
            return
        w = LarkCmdWorker(["auth", "logout", "--json"], 30)
        self._logout_worker = w
        w.done.connect(self._logout_done)
        w.start()

    def _logout_done(self, res: dict) -> None:
        self._append(f"[{datetime.now():%H:%M:%S}] "
                     + ("已登出" if res.get("ok")
                        else f"登出失败：{res.get('error', '?')}"))
        self.refresh_status()

    # ---------------- 周期刷新 ----------------
    def refresh(self) -> None:
        """低频自动刷新状态（60 秒一次；登录轮询期间不并发查询）。"""
        self._tick += 1
        if self._tick >= 60:
            self._tick = 0
            if self._poll_worker is None or not self._poll_worker.isRunning():
                self.refresh_status()


# ---------------------------------------------------------------- 页：云端

class CloudPage(QWidget):
    def __init__(self, win: "Console"):
        super().__init__()
        self.win = win
        v = QVBoxLayout(self)

        gb = QGroupBox("流水线")
        f = QVBoxLayout(gb)
        self.lbl = QLabel("—"); self.lbl.setWordWrap(True)
        f.addWidget(self.lbl)
        self.cur = QLabel(""); self.cur.setWordWrap(True)
        self.cur.setStyleSheet(f"color:{BLUE};")
        f.addWidget(self.cur)
        v.addWidget(gb)

        gb2 = QGroupBox("最近处理结果")
        f2 = QVBoxLayout(gb2)
        self.recent = QPlainTextEdit(); self.recent.setReadOnly(True)
        self.recent.setMaximumHeight(150)
        f2.addWidget(self.recent)
        v.addWidget(gb2)

        gb3 = QGroupBox("报告（txt 双击在线查看，PDF/HTML 双击下载后打开）")
        f3 = QVBoxLayout(gb3)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["课程 / 日期", "大小", "生成时间"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.itemDoubleClicked.connect(self._view)
        f3.addWidget(self.table)
        row = QHBoxLayout()
        self.status = QLabel(""); row.addWidget(self.status, 1)
        b = QPushButton("刷新"); b.clicked.connect(self.win.refresh_reports)
        row.addWidget(b)
        b2 = QPushButton("打开本地报告目录")
        b2.clicked.connect(lambda: subprocess.Popen(["explorer", str(REPORTS_DIR)]))
        row.addWidget(b2)
        f3.addLayout(row)
        v.addWidget(gb3)

    def _view(self, item) -> None:
        row = item.row()
        if row >= len(self.win.reports):
            return
        self.win.view_report(self.win.reports[row]["key"])

    def refresh(self) -> None:
        st = self.win.pipe_status or {}
        h = self.win.health.get("pipeline") or {}
        if st:
            sync = st.get("schedule_sync") or {}
            ch = st.get("cloud_health") or {}
            if ch:
                cloud_line = (f"云端读取：{'✅' if ch.get('ok') else '❌'} "
                              f"{ch.get('at', '—')}"
                              + (f"　{ch.get('error')}" if ch.get("error") else ""))
            else:
                cloud_line = "云端读取：（尚未探测，需运行新版流水线）"
            self.lbl.setText(
                f"状态：{h.get('state', '?')}　PID {st.get('pid', '?')}　"
                f"启动于 {st.get('started_at', '?')}\n"
                f"轮询间隔 {st.get('interval_sec', '?')}s　已轮询 {st.get('rounds', 0)} 轮　"
                f"累计处理 {st.get('processed_total', 0)} 门课\n"
                f"最近扫描：{st.get('last_scan_at', '?')}　待处理 {st.get('pending_count', 0)} 门\n"
                f"课表同步：{'✅' if sync.get('ok') else '❌'} {sync.get('at', '—')}"
                + (f"　{sync.get('error')}" if sync.get("error") else "")
                + "\n" + cloud_line)
        else:
            self.lbl.setText(
                f"状态：{h.get('state', '?')}　{h.get('detail', '')}\n"
                "（流水线未升级到写状态文件的版本，或未运行）")
        cur = st.get("current") or {}
        wait = st.get("waiting") or []
        self.cur.setText(
            (f"▶ 正在处理：{cur.get('meta_key', '')}（{cur.get('started_at', '')} 开始）\n"
             if cur else "")
            + (f"⏳ 等宽限期：{len(wait)} 门（分段可能还在上传）" if wait else ""))
        rec = st.get("recent") or []
        self.recent.setPlainText(
            "\n".join(
                f"{'✅' if r.get('ok') else '❌'} {r.get('at', '')} "
                f"{Path(str(r.get('meta_key', ''))).name[:56]}"
                f"　{r.get('status', '')}（{r.get('elapsed_sec', '?')}s）"
                for r in rec) or "（本次会话还没有处理记录）")
        if self.win.reports_error:
            self.status.setText("报告列表加载失败：" + self.win.reports_error[:120])
        else:
            self.status.setText(f"共 {len(self.win.reports)} 份报告"
                                + ("　（加载中…）" if self.win.reports_loading else ""))


# ---------------------------------------------------------------- 页：提示词

class PromptPage(QWidget):
    """大模型提示词 / 报告模板编辑 —— 决定「导出的报告写什么、长什么样」。

    四个可编辑对象：
      * 教评报告主提示词   → **内容**：上课内容、知识小结、知识点、错误分析、时间分配
      * 长课分片摘要       → 转写超 8000 字时先分片摘要的中间步骤
      * 会议纪要           → 课表判定为「会议」时走的另一条分支
      * 文字版报告骨架     → **格式**：导出 txt 的排版壳（发给家长/微信那份）

    保存即写 cloud/prompts.json，**下一门课立即生效**，无需重启流水线。
    """

    def __init__(self, win: "Console"):
        super().__init__()
        self.win = win
        self.key: str | None = None
        self._status: dict = {}
        self._loaded = ""        # 载入时的原文，用于判断「有未保存改动」
        self._loading = False    # setPlainText 会触发 textChanged，需屏蔽

        v = QVBoxLayout(self)

        gb = QGroupBox("选择对象")
        h = QHBoxLayout(gb)
        self.list = QListWidget()
        self.list.setMaximumWidth(250)
        self.list.currentRowChanged.connect(self._on_pick)
        h.addWidget(self.list)
        col = QVBoxLayout()
        self.desc = QLabel(""); self.desc.setWordWrap(True)
        col.addWidget(self.desc)
        self.path = QLabel(""); self.path.setWordWrap(True)
        self.path.setStyleSheet(f"color:{GREY};")
        col.addWidget(self.path)
        col.addStretch(1)
        h.addLayout(col, 1)
        v.addWidget(gb)

        gb2 = QGroupBox("模板内容（可直接编辑）")
        f2 = QVBoxLayout(gb2)
        self.edit = QPlainTextEdit()
        self.edit.setFont(QFont("Consolas", 10))
        self.edit.textChanged.connect(self._on_edit)
        f2.addWidget(self.edit, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("可用数据（点击插入到光标处）："))
        self.var_row = QHBoxLayout()
        row.addLayout(self.var_row)
        row.addStretch(1)
        f2.addLayout(row)
        v.addWidget(gb2, 1)

        gb3 = QGroupBox("操作")
        f3 = QVBoxLayout(gb3)
        row3 = QHBoxLayout()
        self.btn_save = QPushButton("保存并应用")
        self.btn_save.clicked.connect(self.save)
        row3.addWidget(self.btn_save)
        for label, slot in (("校验", self.check_only),
                            ("预览效果", self.preview),
                            ("恢复默认", self.reset),
                            ("打开配置文件位置", self.open_dir)):
            b = QPushButton(label); b.clicked.connect(slot)
            row3.addWidget(b)
        row3.addStretch(1)
        f3.addLayout(row3)
        tip = QLabel("提示：保存后**不用重启**，下一门课就用新提示词。"
                     "改动只影响之后生成的报告，已生成的报告不会变。")
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color:{GREY};")
        f3.addWidget(tip)
        v.addWidget(gb3)

        self.msg = QLabel(""); self.msg.setWordWrap(True)
        v.addWidget(self.msg)

    # ---------------- 内部 ----------------

    def _say(self, text: str, color: str = "") -> None:
        self.msg.setText(text)
        self.msg.setStyleSheet(f"color:{color};" if color else "")

    def _rebuild_list(self) -> None:
        st = prompts_mod.status()
        self.list.blockSignals(True)
        self.list.clear()
        for it in st["items"]:
            mark = " ●已改" if it["modified"] else ""
            self.list.addItem(f"{it['name']}{mark}")
        self.list.blockSignals(False)
        self._status = st
        if self.key:
            keys = [i["key"] for i in st["items"]]
            if self.key in keys:
                self.list.setCurrentRow(keys.index(self.key))
        elif st["items"]:
            self.list.setCurrentRow(0)

    def _rebuild_vars(self) -> None:
        while self.var_row.count():
            w = self.var_row.takeAt(0).widget()
            if w:
                w.deleteLater()
        meta = prompts_mod.DEFAULTS.get(self.key or "", {})
        for name in meta.get("vars", []):
            b = QPushButton("{%s}" % name)
            b.setFlat(True)
            b.setStyleSheet(f"color:{BLUE};text-align:left;padding:0 6px;")
            b.setToolTip(f"把 {{{name}}} 插入到光标处")
            b.clicked.connect(lambda _=False, n=name: self._insert("{%s}" % n))
            self.var_row.addWidget(b)

    def _insert(self, text: str) -> None:
        self.edit.insertPlainText(text)
        self.edit.setFocus()

    def _on_pick(self, row: int) -> None:
        items = (self._status or {}).get("items") or []
        if row < 0 or row >= len(items):
            return
        new_key = items[row]["key"]
        if new_key == self.key:
            return
        if self.key and self.edit.toPlainText() != self._loaded:
            ans = QMessageBox.question(
                self, "未保存的修改",
                f"「{prompts_mod.DEFAULTS[self.key]['name']}」有未保存的修改，"
                "切换后会被丢弃。继续？")
            if ans != QMessageBox.StandardButton.Yes:
                keys = [i["key"] for i in items]
                self.list.blockSignals(True)
                self.list.setCurrentRow(keys.index(self.key))
                self.list.blockSignals(False)
                return
        self._load(new_key)

    def _load(self, key: str) -> None:
        self.key = key
        meta = prompts_mod.DEFAULTS[key]
        self.desc.setText(meta["desc"])
        self._loading = True
        self.edit.setPlainText(prompts_mod.get_prompt(key))
        self._loading = False
        self._loaded = self.edit.toPlainText()
        self._rebuild_vars()
        self._mark()

    def _mark(self) -> None:
        text = self.edit.toPlainText()
        dirty = text != self._loaded
        key = self.key or ""
        miss_req = prompts_mod.missing_vars(key, text, True)
        miss_opt = prompts_mod.missing_vars(key, text, False)
        parts = [f"字数 {len(text)}",
                 "● 有未保存修改" if dirty else "（与已保存内容一致）"]
        if miss_req:
            parts.append("⚠ 缺必需数据：" + "、".join("{%s}" % m for m in miss_req))
        elif miss_opt:
            parts.append("未使用：" + "、".join("{%s}" % m for m in miss_opt))
        self._say("　".join(parts),
                  RED if miss_req else (ORANGE if (dirty or miss_opt) else GREY))
        self.btn_save.setEnabled(bool(self.key))
        self.btn_save.setText("保存并应用" + (" ●" if dirty else ""))

    def _on_edit(self) -> None:
        if not self._loading:
            self._mark()

    # ---------------- 按钮 ----------------

    def save(self) -> None:
        if not self.key:
            return
        ok, msg = prompts_mod.save(self.key, self.edit.toPlainText())
        if not ok:
            self._say("❌ 未保存：" + msg, RED)
            QMessageBox.warning(self, "校验未通过", msg + "\n\n已保留你刚才的编辑，未写入文件。")
            return
        self._loaded = self.edit.toPlainText()
        self._rebuild_list()
        self._mark()
        if msg:
            self._say("✅ 已保存并生效　" + msg.replace("\n", "　"), ORANGE)
        else:
            self._say("✅ 已保存并生效，下一门课即使用新提示词", GREEN)

    def check_only(self) -> None:
        if not self.key:
            return
        ok, msg = prompts_mod.validate(self.key, self.edit.toPlainText())
        if not ok:
            self._say("❌ " + msg, RED)
            QMessageBox.warning(self, "校验未通过", msg)
        elif msg:
            self._say("⚠ 可保存，但有提醒：\n" + msg, ORANGE)
            QMessageBox.information(self, "可保存", msg)
        else:
            self._say("✅ 校验通过，可以直接保存", GREEN)
            QMessageBox.information(self, "校验通过", "占位符齐全，预览渲染正常。")

    def preview(self) -> None:
        if not self.key:
            return
        text = self.edit.toPlainText()
        ok, msg = prompts_mod.validate(self.key, text)
        if not ok:
            QMessageBox.warning(self, "无法预览", msg)
            return
        try:
            out = prompts_mod.preview(self.key, text)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "预览失败", str(exc))
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"预览 · {prompts_mod.DEFAULTS[self.key]['name']}"
                           "（用示例数据渲染，不消耗任何费用）")
        dlg.resize(900, 700)
        lay = QVBoxLayout(dlg)
        head = QLabel("这就是实际发给大模型的全文（示例数据已填入占位符）。"
                      + (f"　{msg}" if msg else ""))
        head.setWordWrap(True)
        head.setStyleSheet(f"color:{ORANGE if msg else GREY};")
        lay.addWidget(head)
        box = QPlainTextEdit(); box.setReadOnly(True)
        box.setFont(QFont("Consolas", 10))
        box.setPlainText(out)
        lay.addWidget(box, 1)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        lay.addWidget(btns)
        dlg.exec()

    def reset(self) -> None:
        if not self.key:
            return
        name = prompts_mod.DEFAULTS[self.key]["name"]
        if QMessageBox.question(
                self, "恢复默认",
                f"把「{name}」恢复为出厂默认内容？\n当前编辑内容会丢失。") \
                != QMessageBox.StandardButton.Yes:
            return
        ok, msg = prompts_mod.reset(self.key)
        if not ok:
            self._say("❌ " + msg, RED)
            return
        self._load(self.key)
        self._rebuild_list()
        self._say("✅ 已恢复默认：" + msg, GREEN)

    def open_dir(self) -> None:
        subprocess.Popen(["explorer", str(prompts_mod.PROMPTS_FILE.parent)])

    # ---------------- 周期刷新 ----------------

    def refresh(self) -> None:
        """只更新「已改」标记与路径，绝不覆盖编辑器内容（否则打字会被周期性冲掉）。"""
        if self.key is None:
            self._rebuild_list()
        else:
            st = prompts_mod.status()
            items = st["items"]
            keys = [i["key"] for i in items]
            self.list.blockSignals(True)
            for i, it in enumerate(items):
                mark = " ●已改" if it["modified"] else ""
                want = f"{it['name']}{mark}"
                if self.list.item(i) and self.list.item(i).text() != want:
                    self.list.item(i).setText(want)
            self.list.blockSignals(False)
            self._status = st
            self.path.setText(
                f"配置文件：{st['file']}"
                + (f"　最后保存 {st['updated_at']}" if st["exists"] else
                   "　（尚未创建 —— 当前全部使用出厂默认）")
                + f"　已改 {st['modified_count']} 项")


# ---------------------------------------------------------------- 主窗口

class Console(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("课堂录制系统 · 控制台")
        self.resize(1080, 900)

        self.svc = ServiceManager()
        self.health: dict = {}
        self.rec_status: dict = {}
        self.pipe_status: dict = {}
        self.upload_status: dict = {}
        self.vad_series: list[float] = []
        self.alerts: list[str] = []
        self.guard_stats = {"rescued": 0, "last": "", "miss": {}}
        self.reports: list[dict] = []
        self.reports_error = ""
        self.reports_loading = False
        self.retry_busy = False
        self.cos_worker: CosWorker | None = None
        self.retry_worker = None
        self._cos_tick = 0
        # 本地分段核验：tag 集合来自 COS（哪些课已完整上传）
        self.uploaded_tags: set[str] = set()
        self.uploaded_error = ""
        self.uploaded_checked_at = ""
        self.tags_worker: UploadedTagsWorker | None = None
        self._tags_tick = 0

        self.cfg = _load_json(RECORDER_DIR / "config.json") or {}
        self.out_dir = Path(self.cfg.get("out_dir", r"C:/recordings"))
        self.report_prefix = ((_load_json(CLOUD_DIR / "cloud_config.json") or {})
                              .get("report", {}).get("result_prefix", "jiaopei/reports/"))
        # 选定的授课教师：内存值（含未落盘的选择）+ config.json 里的已保存值
        self.selected_teacher = (self.cfg.get("selected_teacher") or "").strip()

        # ---- 顶部：服务灯 + 时钟 ----
        root = QWidget(); self.setCentralWidget(root)
        v = QVBoxLayout(root)
        bar = QHBoxLayout()
        self.title = QLabel("课堂录制系统")
        self.title.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        bar.addWidget(self.title)
        bar.addSpacing(12)
        self.lights = {}
        for key, svc in SERVICES.items():
            lab = QLabel("●"); lab.setFont(QFont("Microsoft YaHei", 14))
            txt = QLabel(svc["title"]); txt.setFont(QFont("Microsoft YaHei", 9))
            bar.addWidget(lab); bar.addWidget(txt); bar.addSpacing(14)
            self.lights[key] = lab
        bar.addStretch(1)
        self.banner = QLabel(""); bar.addWidget(self.banner)
        self.clock = QLabel("--")
        self.clock.setFont(QFont("Microsoft YaHei", 10))
        bar.addWidget(self.clock)
        v.addLayout(bar)

        self.tabs = QTabWidget()
        self.pages = {
            "总览": OverviewPage(self),
            "录课": RecordPage(self),
            "麦克风": AudioPage(self),
            "提示词": PromptPage(self),
            "云端": CloudPage(self),
            "飞书": FeishuPage(self),
        }
        for name, page in self.pages.items():
            self.tabs.addTab(page, name)
        v.addWidget(self.tabs, 1)

        self.timer = QTimer(self); self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.guard_timer = QTimer(self); self.guard_timer.setInterval(15000)
        self.guard_timer.timeout.connect(self.guard)
        self.guard_timer.start()
        self.tick()
        QTimer.singleShot(600, self.refresh_reports)
        QTimer.singleShot(800, self.check_uploaded)

    # ---------------- 数据读取 ----------------
    def teacher_choices(self) -> list[str]:
        """教师候选：cloud_config 的 feishu.teacher_names + 今日课表出现的教师。"""
        names: list[str] = []
        cfg_feishu = (_load_json(CLOUD_DIR / "cloud_config.json") or {}).get("feishu") or {}
        raw = cfg_feishu.get("teacher_names")
        if isinstance(raw, (list, tuple)):
            names += [str(x).strip() for x in raw if str(x).strip()]
        elif isinstance(raw, str) and raw.strip():
            names.append(raw.strip())
        single = (cfg_feishu.get("teacher_name") or "").strip()
        if single:
            names.append(single)
        for it in (_load_json(RECORDER_DIR / "lessons.json") or []):
            t = (it.get("teacher") or "").strip()
            if t and t != "未知":
                names.append(t)
        seen: set[str] = set()
        out: list[str] = []
        for n in names:
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out

    def set_selected_teacher(self, name: str) -> None:
        """应用授课教师：下发录课服务（热生效）+ 写回 config.json（重启生效）。"""
        name = (name or "").strip()
        self.selected_teacher = name
        self.cfg["selected_teacher"] = name
        ok = True
        try:
            path = RECORDER_DIR / "config.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["selected_teacher"] = name
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            ok = False
            QMessageBox.warning(self, "写入失败",
                                f"selected_teacher 写回 config.json 失败：\n{exc}")
        sent = self.send_control({"action": "set_teacher", "teacher": name})
        if ok and sent:
            QMessageBox.information(
                self, "已应用",
                f"授课教师已设为「{name}」。\n"
                "· 录课服务已热生效（VAD / 手动录制按此上报）\n"
                "· 重启录课服务后仍生效（已写入 config.json）")

    def local_groups(self) -> dict:
        """按课程 tag 分组本地 wav 分段（排除仍在录制中的那一组）。"""
        pat = re.compile(r"^(.+_\d{8}_\d{6})_\d{5}\.wav$")
        window = int(self.cfg.get("segment_seconds", 300)) + 120
        now = time.time()
        groups: dict[str, list[Path]] = {}
        try:
            for wav in self.out_dir.glob("*.wav"):
                m = pat.match(wav.name)
                if m:
                    groups.setdefault(m.group(1), []).append(wav)
        except Exception:  # noqa: BLE001
            pass
        return {t: s for t, s in groups.items()
                if now - max(x.stat().st_mtime for x in s) >= window}

    def local_summary(self) -> dict:
        """把本地分段分成「已上传·保留中」与「未核验上传·真残留」两类。

        核验依据 = COS 上是否存在该 tag 的 meta.json（一次 list 调用，
        后台线程做，不阻塞界面）。核验失败时一律标为未知并禁用补传 ——
        宁可不动，也不要重传 1GB。
        """
        pending, retained = [], []
        for tag, files in self.local_groups().items():
            item = {"tag": tag, "n": len(files),
                    "size": sum(f.stat().st_size for f in files)}
            (retained if tag in self.uploaded_tags else pending).append(item)
        known = not self.uploaded_error and bool(self.uploaded_checked_at)
        return {
            "pending": pending, "retained": retained,
            "pending_n": sum(i["n"] for i in pending),
            "pending_bytes": sum(i["size"] for i in pending),
            "retained_n": sum(i["n"] for i in retained),
            "retained_bytes": sum(i["size"] for i in retained),
            "known": known, "error": self.uploaded_error,
            "checked_at": self.uploaded_checked_at,
        }

    def check_uploaded(self) -> None:
        """后台核验本地分段在 COS 上的上传状态（启动时 + 每 10 分钟 + 手动）。"""
        if self.tags_worker is not None and self.tags_worker.isRunning():
            return
        cos_cfg = self.cfg.get("cos")
        if not cos_cfg:
            self.uploaded_error = "未找到 COS 配置"
            return
        self.tags_worker = UploadedTagsWorker(
            cos_cfg, self.cfg.get("org_id", "org"), self.cfg.get("room_id", "room-01"))
        self.tags_worker.done.connect(self._uploaded_done)
        self.tags_worker.start()

    def _uploaded_done(self, tags: set, err: str) -> None:
        self.uploaded_error = err
        self.uploaded_checked_at = datetime.now().strftime("%H:%M:%S")
        if not err:
            self.uploaded_tags = tags

    # ---------------- 每秒刷新 ----------------
    def tick(self) -> None:
        now = datetime.now()
        self.clock.setText(now.strftime("%Y-%m-%d %H:%M:%S"))
        try:
            CONSOLE_HEARTBEAT.write_text(now.strftime("%Y-%m-%d %H:%M:%S"),
                                         encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

        self.health = {k: self.svc.health(k) for k in SERVICES}
        for k, h in self.health.items():
            _light(self.lights[k], h["state"], h["detail"])

        self.rec_status = _load_json(STATUS_DIR / "recorder_status.json") or {}
        self.upload_status = _load_json(STATUS_DIR / "upload_status.json") or {}
        self.pipe_status = _load_json(STATUS_DIR / "pipeline_status.json") or {}

        # VAD 电平序列：给电平柱画历史（状态文件里只有 10 秒统计，这里自行累积）
        lvl = ((self.rec_status.get("audio") or {}).get("level") or {})
        if lvl.get("n"):
            self.vad_series.append(float(lvl.get("rms") or 0.0))
            self.vad_series = self.vad_series[-200:]

        self.banner.setText("")
        self.banner.setStyleSheet("")
        bad = [h["title"] for h in self.health.values() if h["state"] == "bad"]
        if bad:
            self.banner.setText(f"⚠ {'、'.join(bad)} 未运行")
            self.banner.setStyleSheet(f"color:{RED};font-weight:bold;")

        self.alerts = self._collect_alerts()
        for page in self.pages.values():
            try:
                page.refresh()
            except Exception:  # noqa: BLE001
                pass

        self._cos_tick += 1
        if self._cos_tick >= 60:
            self._cos_tick = 0
            self.refresh_reports()
        self._tags_tick += 1
        if self._tags_tick >= 600:
            self._tags_tick = 0
            self.check_uploaded()

    def _collect_alerts(self) -> list[str]:
        out: list[str] = []
        for k, h in self.health.items():
            if h["state"] == "bad":
                out.append(f"{h['title']}：{h['detail']}")
            elif h["state"] == "warn":
                out.append(f"{h['title']}：{h['detail']}")
        rec = self.rec_status or {}
        audio = rec.get("audio") or {}
        lvl = audio.get("level") or {}
        thr = float(audio.get("voice_rms") or 0.02)
        if audio.get("vad_active") and lvl.get("n", 0) > 30:
            p95 = float(lvl.get("p95") or 0)
            if p95 < thr * 0.3:
                out.append(f"麦克风电平偏低（p95 {p95:.4f} 远低于阈值 {thr}）"
                           f"→ VAD 课不会自动开录，到「麦克风」页校准")
            elif p95 > thr * 20:
                out.append(f"环境噪声偏高（p95 {p95:.4f} 远超阈值 {thr}）"
                           f"→ VAD 可能从早录到晚")
        up = self.upload_status or {}
        if up.get("state") == "failed":
            out.append(f"上传失败：{up.get('course_id')} —— {(up.get('error') or '')[:80]}")
        st = self.pipe_status or {}
        lr = st.get("last_result") or {}
        if lr and not lr.get("ok"):
            out.append(f"云端处理失败：{str(lr.get('status'))[:90]}")
        # 云端读可达性（欠费/权限故障 ListObjects 看不出来，必须靠主动探测）
        ch = st.get("cloud_health") or {}
        if ch and not ch.get("ok"):
            out.append(f"云端读取不通（{ch.get('stage', 'get')}）："
                       f"{str(ch.get('error'))[:90]} → 检查腾讯云账户余额与权限")
        # 课表有课但没在录
        rl = rec.get("lesson") or {}
        if rec.get("state") != "recording":
            for it in (_load_json(RECORDER_DIR / "lessons.json") or []):
                s = _parse_dt(it.get("start", "")); e = _parse_dt(it.get("end", ""))
                if s and e and s <= datetime.now() <= e and (datetime.now() - s).total_seconds() > 300:
                    out.append(f"「{it.get('course_id')}」应在录制中，但录制服务已待机")
                    break
        return out

    # ---------------- 服务托管 ----------------
    def start_service(self, key: str) -> None:
        ok, msg = self.svc.start(key)
        if not ok:
            QMessageBox.warning(self, "启动失败", msg)

    def restart_service(self, key: str) -> None:
        if QMessageBox.question(
                self, "重启服务",
                f"确定重启{SERVICES[key]['title']}？\n"
                + ("（当前正在录制，重启会中断这节课！）\n"
                   if (self.rec_status.get("state") == "recording" and key == "recorder")
                   else "")
                + "继续？") != QMessageBox.StandardButton.Yes:
            return
        ok, msg = self.svc.restart(key)
        if not ok:
            QMessageBox.warning(self, "重启失败", msg)

    def start_all(self) -> None:
        """一键拉起全部服务。

        已经在正常运行（state=ok）的跳过：否则重复启动虽会被单实例锁挡住，
        返回消息仍是「已发出启动」，会让人误以为"刚把它拉起来了"。
        注意只跳过 ok —— warn/bad 照常启动，不然僵死/掉线就救不回来。
        """
        msgs = []
        for k in SERVICES:
            if self.svc.health(k).get("state") == "ok":
                msgs.append(f"{SERVICES[k]['title']}：运行中，跳过")
                continue
            msgs.append(f"{SERVICES[k]['title']}：{self.svc.start(k)[1]}")
        QMessageBox.information(self, "全部拉起", "\n".join(msgs))

    def guard(self) -> None:
        """自动守护：连续 2 次判定 bad 才动手，避免启动瞬间被误拉。"""
        if not self.pages["总览"].chk_guard.isChecked():
            return
        for key in SERVICES:
            h = self.svc.health(key)
            if h["state"] == "bad":
                miss = self.guard_stats["miss"].get(key, 0) + 1
                self.guard_stats["miss"][key] = miss
                if miss >= 2:
                    ok, msg = self.svc.start(key)
                    if ok:
                        self.guard_stats["rescued"] += 1
                        self.guard_stats["last"] = (
                            f"{datetime.now():%H:%M:%S} 自动拉起{SERVICES[key]['title']}")
                        _log_guard(f"自动守护触发：{msg}")
                    self.guard_stats["miss"][key] = 0
            else:
                self.guard_stats["miss"][key] = 0

    # ---------------- 控制指令 ----------------
    def send_control(self, payload: dict) -> bool:
        try:
            STATUS_DIR.mkdir(parents=True, exist_ok=True)
            payload["ts"] = datetime.now().isoformat(timespec="seconds")
            tmp = CONTROL_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(CONTROL_FILE)
            return True
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "发送失败", f"无法写入控制指令：\n{exc}")
            return False

    def manual_start(self) -> None:
        dlg = ManualStartDialog(self)
        # 教师预填「录课」页下拉栏选定的授课教师（一致归属，少敲一次）
        if self.selected_teacher:
            dlg.ed_teacher.setText(self.selected_teacher)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        course, teacher = dlg.values()
        if self.send_control({"action": "start", "course_id": course,
                              "teacher": teacher}):
            QMessageBox.information(self, "已下发", f"开始录制「{course}」。")

    def manual_stop(self) -> None:
        course = ((self.rec_status or {}).get("lesson") or {}).get("course_id", "")
        if QMessageBox.question(
                self, "停止录制",
                f"确定停止当前录制{f'（{course}）' if course else ''}并上传？") \
                != QMessageBox.StandardButton.Yes:
            return
        self.send_control({"action": "stop"})

    # ---------------- 上传补传 ----------------
    def retry_upload(self) -> None:
        summ = self.local_summary()
        if not summ["known"]:
            QMessageBox.warning(
                self, "无法核验",
                "还没能向 COS 确认哪些课程已上传，不能贸然补传\n"
                "（避免把已上传的音频重传一遍）。\n\n"
                f"上次核验结果：{summ.get('error') or '尚未执行'}\n\n"
                "点「重新核验」或检查 COS 可用性后再试。")
            return
        pend = summ["pending"]
        if not pend:
            QMessageBox.information(self, "无需补传", "没有未上传的本地分段。")
            return
        groups = {i["tag"]: self.local_groups()[i["tag"]] for i in pend}
        if QMessageBox.question(
                self, "确认补传",
                f"本地有 {summ['pending_n']} 个分段"
                f"（{_fmt_bytes(summ['pending_bytes'])}，{len(pend)} 节课）"
                "在 COS 上没有 meta，将在上传后删除本地副本，继续？") \
                != QMessageBox.StandardButton.Yes:
            return
        from monitor_gui import RetryWorker
        self.retry_busy = True
        self.retry_worker = RetryWorker(self.cfg, groups)
        self.retry_worker.done.connect(self._retry_done)
        self.retry_worker.start()

    def _retry_done(self, msg: str, err: str) -> None:
        self.retry_busy = False
        if err:
            QMessageBox.warning(self, "补传失败", err)
        else:
            QMessageBox.information(self, "补传完成", msg)

    # ---------------- 报告 ----------------
    def refresh_reports(self) -> None:
        cos_cfg = self.cfg.get("cos")
        if not cos_cfg:
            self.reports_error = "未找到 COS 配置"
            return
        if self.cos_worker is not None and self.cos_worker.isRunning():
            return
        self.reports_loading = True
        self.reports_error = ""
        self.cos_worker = CosWorker(cos_cfg, self.report_prefix)
        self.cos_worker.list_done.connect(self._reports_loaded)
        self.cos_worker.text_done.connect(self._report_text)
        self.cos_worker.file_done.connect(self._report_file)
        self.cos_worker.list_reports()

    def _reports_loaded(self, items: list, err: str) -> None:
        self.reports_loading = False
        if err:
            self.reports_error = err
            return
        self.reports = items
        tbl = self.pages["云端"].table
        tbl.setRowCount(len(items))
        for row, it in enumerate(items):
            name = Path(it["key"]).name.replace("_report.txt", "").replace(".txt", "")
            tbl.setItem(row, 0, QTableWidgetItem(name))
            tbl.setItem(row, 1, QTableWidgetItem(_fmt_bytes(it["size"])))
            tbl.setItem(row, 2, QTableWidgetItem(it["mtime"].replace("T", " ")[:19]))

    def _ensure_worker(self) -> None:
        if self.cos_worker is None or not self.cos_worker.isRunning():
            self.cos_worker = CosWorker(self.cfg["cos"], self.report_prefix)
            self.cos_worker.list_done.connect(self._reports_loaded)
            self.cos_worker.text_done.connect(self._report_text)
            self.cos_worker.file_done.connect(self._report_file)

    def view_report(self, key: str) -> None:
        self._ensure_worker()
        if key.endswith(".txt"):
            self.cos_worker.fetch_text(key)
        else:
            self.cos_worker.fetch_file(key)

    def _report_text(self, key: str, text: str, err: str) -> None:
        if err:
            QMessageBox.warning(self, "读取失败", err)
            return
        ReportDialog(self, Path(key).name, text).exec()

    def _report_file(self, key: str, local_path: str, err: str) -> None:
        if err:
            QMessageBox.warning(self, "下载失败", err)
            return
        try:
            os.startfile(local_path)  # noqa: S606
        except Exception as exc:  # noqa: BLE001
            QMessageBox.information(self, "已下载", f"{local_path}\n（自动打开失败：{exc}）")


# ---------------------------------------------------------------- 入口

def _acquire_singleton_lock(name: str):
    import msvcrt
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(STATUS_DIR / f"{name}.lock", "a+b")
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
    app = QApplication(sys.argv)
    lock = _acquire_singleton_lock("console")
    if lock is None:
        QMessageBox.information(None, "已在运行", "控制台已经开着一个窗口了。")
        return 0
    win = Console()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
