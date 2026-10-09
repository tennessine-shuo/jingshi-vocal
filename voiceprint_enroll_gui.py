# -*- coding: utf-8 -*-
"""声纹注册 GUI（教培 1 对 1 教评系统配套工具）。

功能：
  - 窗口内录音（ffmpeg dshow，与正式录制同一采集链路：本机麦克风 → 16kHz 单声道 wav）
  - 开始 / 停止录音、计时、最长 30 秒自动停止
  - 录音质量自检（时长 3~30s、音量检测）
  - 试听 / 重录
  - 调用腾讯云 VoicePrintEnroll 注册到 1:N 分组，并写入本地注册库
  - 已注册人员列表查看

依赖：PySide6、tencentcloud-sdk-python、ffmpeg、winsound（标准库）
启动：start_voiceprint_gui.bat 或
      C:\\recorder\\venv\\Scripts\\python.exe C:\\recorder\\voiceprint_enroll_gui.py
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
import time
import wave
import winsound
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

REC_CONFIG = Path(r"C:\recorder\recorder\config.json")
CLOUD_CONFIG = Path(r"C:\recorder\cloud\cloud_config.json")
TMP_DIR = Path(r"C:\recorder\cloud_work\enroll_tmp")
SAMPLE_DIR = Path(r"C:\recorder\cloud_work\enroll_samples")
CLOUD_DIR = Path(r"C:\recorder\cloud")
CLASSROOM_DIR = Path(r"C:\recorder\cloud_work")   # 课堂片段（远场）根目录

MIN_SEC, MAX_SEC = 3, 30

GUIDE_TEXT = (
    "请对着麦克风，用正常上课的音量朗读：\n"
    "“同学们好，我们今天来学习新的内容。"
    "先回顾一下上节课讲过的知识点，"
    "大家还记得这个概念是怎么定义的吗？”"
)

# ---------------------------------------------------------------------------
# 两套声纹库的关系（改代码前务必读懂）
#
# 云端库（腾讯 VoicePrint）与本地库（CAM++ ONNX）是**互不相通的两套向量空间**：
# 腾讯用自己的私有模型，本地用 3D-Speaker CAM++，「云端的向量无法迁移到本地」，
# 同一个人必须在两边各自注册一次。
#
# 更要紧的是域敏感度不同：
#   · 云端模型抗域偏移好 —— 近场耳麦录一段就能用，本工具录的正合适。
#   · 本地 CAM++ 对声学域极敏感 —— 「近场注册 + 远场课堂比对」会让同人余弦
#     从 0.8 掉到 0.3，区分度直接崩塌。本地库的注册素材必须是**课堂远场片段**。
#
# 所以本工具对两套库采取不同策略：云端随手更新，本地只做「有提示的补录」，
# 绝不静默把近场录音塞进本地库。
# ---------------------------------------------------------------------------
DOMAIN_WARN = (
    "⚠ 这段录音来自近场耳麦。本地声纹库要求「与使用场景同域」——\n"
    "近场样本配远场课堂音频，同人余弦会从 0.8 掉到 0.3，反而拉低识别率。\n"
    "只有在确实没有课堂片段时才勾选；更推荐用「补录到本地库」选课堂片段。"
)

# 实测证据（2026-09-15，用三个人在 enroll_samples 里的留存样本跑本地 CAM++）：
#   学生戊 的片段 ↔ 学生丙 = 0.552，而本地阈值 local_threshold 就是 0.50
#   学生戊 ↔ 学生甲 = 0.202，学生丙 ↔ 学生甲 = 0.242
# 即：同一支近场麦录的干净语音，声学条件雷同，不同人之间余弦反而虚高，
# 已越过阈值 —— 近场样本批量入库会互相误认。
DOMAIN_EVIDENCE = (
    "实测（2026-09-15，三人留存样本跑本地模型）：\n"
    "　学生戊 ↔ 学生丙 = 0.552，已超过本地阈值 0.50，同时入库会互相误认；\n"
    "　学生戊 ↔ 学生甲 = 0.202、学生丙 ↔ 学生甲 = 0.242，这两对倒是安全。\n"
    "结论：近场样本能不用就不用，优先课堂远场片段。"
)


def find_ffmpeg() -> str:
    bundled = Path(__file__).resolve().parent / "ffmpeg" / "bin" / "ffmpeg.exe"
    for cand in (bundled, Path(r"C:\ffmpeg\bin\ffmpeg.exe")):
        if cand.exists():
            return str(cand)
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    raise FileNotFoundError("未找到 ffmpeg.exe（C:\\ffmpeg\\bin 或 PATH）")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 本地声纹库

_LOCAL_DB = None            # LocalVoiceprint 句柄缓存（模型只加载一次）
_LOCAL_DB_MTIME = 0.0


def local_paths(cloud_cfg: dict) -> tuple[str, str]:
    """从 cloud_config.json 取本地模型与库路径（带兜底，避免配置缺键就崩）。"""
    vp = cloud_cfg.get("voiceprint", {}) or {}
    model = vp.get("local_model") or r"C:\recorder\models\campplus_sv_zh_16k.onnx"
    db = vp.get("local_db") or str(CLOUD_DIR / "voiceprint_local_db.json")
    return str(model), str(db)


def read_local_persons(cloud_cfg: dict) -> dict:
    """只读 JSON 拿「本地库里有谁」（不加载 27MB 模型，供列表刷新秒回）。

    返回 {person_id: {"n_samples": n, "person_name": name}}。
    """
    _, db = local_paths(cloud_cfg)
    p = Path(db)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for pid, rec in (data.get("persons") or {}).items():
        out[pid] = {"n_samples": len(rec.get("embeddings") or []),
                    "person_name": rec.get("person_name", "")}
    return out


def get_local_db(cloud_cfg: dict):
    """懒加载本地声纹库句柄。

    CAM++ 模型 27MB、加载约 1~2 秒，只在真要写库时（且必然在后台线程里）才建。
    库文件被外部脚本改过时只重读 JSON，不重建模型 —— 重建代价高且没必要。
    """
    global _LOCAL_DB, _LOCAL_DB_MTIME
    model, db = local_paths(cloud_cfg)
    p = Path(db)
    mtime = p.stat().st_mtime if p.exists() else 0.0
    if _LOCAL_DB is None:
        if str(CLOUD_DIR) not in sys.path:
            sys.path.insert(0, str(CLOUD_DIR))
        from voiceprint_local import LocalVoiceprint
        _LOCAL_DB = LocalVoiceprint(model, db)
        _LOCAL_DB_MTIME = mtime
    elif mtime != _LOCAL_DB_MTIME:
        try:
            _LOCAL_DB._load()
            _LOCAL_DB_MTIME = mtime
        except Exception:  # noqa: BLE001
            pass
    return _LOCAL_DB


def build_asr_client(cloud_cfg: dict):
    """构造腾讯云 ASR 客户端（注册 / 原地更新 / 昵称同步三处共用）。"""
    from tencentcloud.asr.v20190614 import asr_client, models
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile

    cos_cfg = cloud_cfg["cos"]
    cred = credential.Credential(cos_cfg["secret_id"], cos_cfg["secret_key"])
    hp = HttpProfile(); hp.endpoint = "asr.tencentcloudapi.com"
    cp = ClientProfile(); cp.httpProfile = hp
    return asr_client.AsrClient(cred, cos_cfg.get("region", "ap-shanghai"), cp), models


class UpdateNickWorker(QThread):
    """后台线程：同步云端声纹昵称（SpeakerNick）。"""

    finished_update = Signal(bool, str)

    def __init__(self, cloud_cfg: dict, voiceprint_id: str, name: str):
        super().__init__()
        self.cloud_cfg = cloud_cfg
        self.voiceprint_id = voiceprint_id
        self.name = name

    def run(self):
        try:
            client, models = build_asr_client(self.cloud_cfg)
            req = models.VoicePrintUpdateRequest()
            req.VoiceFormat = 1
            req.SampleRate = 16000
            req.VoicePrintId = self.voiceprint_id
            req.SpeakerNick = self.name
            client.VoicePrintUpdate(req)
            self.finished_update.emit(True, f"云端昵称已同步为：{self.name}")
        except Exception as exc:  # noqa: BLE001
            self.finished_update.emit(
                False,
                f"云端昵称同步失败（不影响报告中的姓名，报告以本地注册库为准）：{exc}")


class EditPersonDialog(QDialog):
    """编辑已注册人员：改资料，或重新录制声纹。

    三件事的语义分清楚，别混：
      · 改名 —— 只动 person_name，云端昵称跟着同步，声纹本身不变
      · 重录 —— 换掉声音本身（云端原地更新，VoicePrintId 不变）
      · 声纹 ID 不可变 —— 它是腾讯云那边的锚点，改了就断链
    """

    RERECORD = 2          # 自定义返回码：调用方据此进入重录模式

    def __init__(self, parent, info: dict, vpid: str = "", local_n: int | None = None):
        super().__init__(parent)
        self.setWindowTitle("编辑已注册人员")
        self.setMinimumWidth(400)
        form = QFormLayout(self)
        self.ed_name = QLineEdit(info.get("person_name", ""))
        self.cb_role = QComboBox()
        self.cb_role.addItems(["学生", "老师"])
        self.cb_role.setCurrentText(
            "老师" if info.get("role") == "teacher" else "学生")
        self.ed_pid = QLineEdit(info.get("person_id", ""))
        form.addRow("姓名：", self.ed_name)
        form.addRow("角色：", self.cb_role)
        form.addRow("人员 ID：", self.ed_pid)
        if vpid:
            lbl_vpid = QLabel(vpid)
            lbl_vpid.setTextInteractionFlags(
                lbl_vpid.textInteractionFlags() | Qt.TextSelectableByMouse)
            lbl_vpid.setStyleSheet("color:#666;")
            form.addRow("声纹 ID：", lbl_vpid)

        tip = QLabel(
            "改名不影响声纹本身；要让系统重新「记住这个人的声音」，"
            "用下面的「重新录制声纹」。")
        tip.setWordWrap(True); tip.setStyleSheet("color:#666;")
        form.addRow(tip)

        if local_n is not None:
            state = (f"本地库已有 {local_n} 段样本" if local_n > 0
                     else "本地库中还没有这个人 —— 录音中识别认不出他")
            lbl_loc = QLabel(state)
            lbl_loc.setWordWrap(True)
            lbl_loc.setStyleSheet(
                "color:#2e7d32;" if local_n > 0 else "color:#c62828;")
            form.addRow("本地库：", lbl_loc)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存资料")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.btn_rerecord = QPushButton("🎙 重新录制声纹")
        self.btn_rerecord.setToolTip(
            "对着麦克风重录一段，覆盖云端声纹（声纹 ID 不变）与本地留存样本")
        self.btn_rerecord.setStyleSheet(
            "QPushButton{background:#1565c0;color:white;padding:6px 12px;"
            "border-radius:4px;}")
        self.btn_rerecord.clicked.connect(lambda: self.done(self.RERECORD))
        buttons.addButton(self.btn_rerecord, QDialogButtonBox.ActionRole)
        form.addRow(buttons)

    def values(self):
        return {
            "person_name": self.ed_name.text().strip(),
            "role": "student" if self.cb_role.currentText() == "学生" else "teacher",
            "person_id": self.ed_pid.text().strip(),
        }


class EnrollWorker(QThread):
    """后台线程：调腾讯云声纹注册 + 写注册库，避免界面卡死。"""

    finished_enroll = Signal(bool, str, str)  # ok, message, voiceprint_id

    def __init__(self, cloud_cfg: dict, wav: Path, person_id: str,
                 name: str, role: str):
        super().__init__()
        self.cloud_cfg = cloud_cfg
        self.wav = wav
        self.person_id = person_id
        self.name = name
        self.role = role

    def run(self):  # noqa: D401
        try:
            client, models = build_asr_client(self.cloud_cfg)
            vp_cfg = self.cloud_cfg.get("voiceprint", {})
            group_id = vp_cfg.get("group_id", "jiaopei_voiceprints")

            data = base64.b64encode(self.wav.read_bytes()).decode()
            req = models.VoicePrintEnrollRequest()
            req.VoiceFormat = 1          # wav
            req.SampleRate = 16000
            req.Data = data
            req.GroupId = group_id
            req.SpeakerNick = self.name  # ≤32 字节
            resp = client.VoicePrintEnroll(req)
            voiceprint_id = resp.Data.VoicePrintId

            # 写本地注册库
            reg_path = Path(vp_cfg.get(
                "registry_file", r"C:\recorder\cloud\voiceprint_registry.json"))
            registry = load_json(reg_path) if reg_path.exists() else {}
            registry[voiceprint_id] = {
                "person_id": self.person_id,
                "person_name": self.name,
                "role": self.role,
            }
            reg_path.write_text(json.dumps(registry, ensure_ascii=False,
                                           indent=2), encoding="utf-8")

            # 留存样本，便于以后重新注册
            SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
            sample = SAMPLE_DIR / f"{self.person_id}_{self.name}.wav"
            shutil.copy2(self.wav, sample)

            self.finished_enroll.emit(
                True,
                f"注册成功：{self.name}（{self.role}）\n"
                f"声纹 ID：{voiceprint_id}\n样本已留存：{sample}",
                voiceprint_id)
        except Exception as exc:  # noqa: BLE001
            self.finished_enroll.emit(False, f"注册失败：{exc}", "")


class ReenrollWorker(QThread):
    """重新录制：云端原地更新 +（可选）重建本地库 + 覆盖留存样本。

    为什么必须走 VoicePrintUpdate 而不是再 Enroll 一次：
      VoicePrintEnroll **每次调用都新建一个 VoicePrintId**。同一个人注册两次，
      云端就出现两条记录 —— 1:N 比对时同一个人被拆成两个候选互相抢票，
      整节课的说话人切分也会错乱。VoicePrintUpdate 传 Data 才是原地换声音。
    """

    finished_reenroll = Signal(bool, str)

    def __init__(self, cloud_cfg: dict, wav: Path, vpid: str, person_id: str,
                 name: str, role: str, update_local: bool):
        super().__init__()
        self.cloud_cfg = cloud_cfg
        self.wav = wav
        self.vpid = vpid
        self.person_id = person_id
        self.name = name
        self.role = role
        self.update_local = update_local

    def run(self):  # noqa: D401
        notes: list[str] = []
        ok_cloud = False

        # 1) 云端：原地更新（VoicePrintId 不变）
        try:
            client, models = build_asr_client(self.cloud_cfg)
            req = models.VoicePrintUpdateRequest()
            req.VoiceFormat = 1
            req.SampleRate = 16000
            req.VoicePrintId = self.vpid
            req.SpeakerNick = self.name
            req.Data = base64.b64encode(self.wav.read_bytes()).decode()
            client.VoicePrintUpdate(req)
            ok_cloud = True
            notes.append("云端声纹已原地更新（声纹 ID 不变，不会产生重复记录）")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"云端更新失败：{exc}")

        # 2) 本地库：替换语义（旧样本会弃用，整库已自动快照）
        if self.update_local:
            try:
                db = get_local_db(self.cloud_cfg)
                r = db.enroll(self.person_id, self.name, self.role,
                              [str(self.wav)], replace=True)
                bak = Path(r["backup"]).name if r.get("backup") else "未生成"
                notes.append(f"本地库已替换：新 {r['added']} 段，"
                             f"弃用旧 {r['dropped']} 段，快照 {bak}")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"本地库更新失败（云端不受影响）：{exc}")

        # 3) 留存样本：同名覆盖，保证「补录」时拿到的是最新那条
        try:
            SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
            sample = SAMPLE_DIR / f"{self.person_id}_{self.name}.wav"
            shutil.copy2(self.wav, sample)
            notes.append(f"留存样本已覆盖：{sample.name}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"留存样本保存失败（不影响声纹）：{exc}")

        self.finished_reenroll.emit(ok_cloud, "\n".join(notes))


class LocalEnrollWorker(QThread):
    """把若干条音频补录进本地声纹库（只动本地，不碰云端）。

    默认追加：本地 1:N 取「与任一注册样本的最大余弦」，多一条同类样本
    只会让命中更稳。replace=True 才是替换（重新录制语义）。
    """

    finished_local = Signal(bool, str)

    def __init__(self, cloud_cfg: dict, jobs: list[dict], replace: bool = False):
        """jobs: [{"person_id","person_name","role","wav_paths"}, ...]（可批量）。

        批量走一个线程是有意为之：CAM++ 模型 27MB，加载一次约 1~2 秒，
        逐个起线程会让每次补录都重付这份开销。
        """
        super().__init__()
        self.cloud_cfg = cloud_cfg
        self.jobs = [{"person_id": j["person_id"],
                      "person_name": j["person_name"],
                      "role": j.get("role", "student"),
                      "wav_paths": [str(p) for p in j["wav_paths"]]}
                     for j in jobs]
        self.replace = replace

    def run(self):  # noqa: D401
        try:
            db = get_local_db(self.cloud_cfg)      # 模型只加载一次，批量复用
        except Exception as exc:  # noqa: BLE001
            self.finished_local.emit(False, f"本地声纹库加载失败：{exc}")
            return

        lines: list[str] = []
        ok_n = 0
        for job in self.jobs:
            label = f"{job['person_name']}（{job['person_id']}）"
            try:
                r = db.enroll(job["person_id"], job["person_name"], job["role"],
                              job["wav_paths"], replace=self.replace)
            except Exception as exc:  # noqa: BLE001
                lines.append(f"✗ {label}：{exc}")
                continue
            ok_n += 1
            lines.append(f"✓ {label} {'替换' if r['replaced'] else '追加'} "
                         f"{r['added']} 段 → 现有 {r['total_embeddings']} 段")
            if r.get("backup"):
                lines.append(f"　　整库快照：{Path(r['backup']).name}")
            for d in r["detail"]:
                if not d.get("ok"):
                    lines.append(f"　　跳过 {Path(d['wav']).name}：{d.get('error')}")
        self.finished_local.emit(ok_n > 0, "\n".join(lines) or "没有可处理的任务")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("声纹注册工具 · 教评系统")
        self.setMinimumSize(760, 800)

        # ---- 配置 ----
        self.ffmpeg = find_ffmpeg()
        rec_cfg = load_json(REC_CONFIG)
        self.audio_fmt = rec_cfg.get("audio_fmt", "dshow")
        self.audio_device = rec_cfg["audio_device"]
        self.cloud_cfg = load_json(CLOUD_CONFIG)
        self.registry_file = Path(self.cloud_cfg.get("voiceprint", {}).get(
            "registry_file", r"C:\recorder\cloud\voiceprint_registry.json"))
        TMP_DIR.mkdir(parents=True, exist_ok=True)

        # ---- 运行状态 ----
        self.proc: subprocess.Popen | None = None
        self.wav_path: Path | None = None
        self.record_start = 0.0
        self.worker: EnrollWorker | None = None
        self.nick_worker: UpdateNickWorker | None = None
        self.rerecord_worker: ReenrollWorker | None = None
        self.local_worker: LocalEnrollWorker | None = None
        # mode: "new" = 给新人注册；"rerecord" = 给已注册的人换声音
        self.mode = "new"
        self.target: dict | None = None      # 重录目标：{vpid, person_id, person_name, role}
        self.local_ids: dict = {}            # person_id → 本地库样本数（刷新时读）
        self.missing: list[dict] = []        # 只在云端、本地库没有的人（刷新时算）

        self._build_ui()
        self._suggest_person_id()
        self._refresh_registry()

        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._tick)

        # 心跳：便于外部确认窗口存活（每秒写一次时间戳）
        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(1000)
        self._heartbeat.timeout.connect(lambda: Path(
            r"C:\recorder\gui_heartbeat.txt").write_text(
            time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8"))
        self._heartbeat.start()

        self.log(f"采集设备：{self.audio_device}")
        self.log("提示：若自动录制服务正在上课录音，请先避开该时段再注册。")

    # ---------------- UI ----------------
    def _build_ui(self):
        root = QWidget(); self.setCentralWidget(root)
        lay = QVBoxLayout(root)

        font = QFont("Microsoft YaHei", 10)
        self.setFont(font)

        # 重录模式横幅（平时隐藏）
        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        self.banner.setStyleSheet(
            "background:#fff3cd; color:#7a4b00; border:1px solid #ffe08a;"
            "border-radius:4px; padding:8px;")
        lay.addWidget(self.banner)

        # 人员信息
        gb_info = QGroupBox("1. 人员信息")
        form = QFormLayout(gb_info)
        self.ed_name = QLineEdit(); self.ed_name.setPlaceholderText("真实姓名，如：张三")
        self.cb_role = QComboBox(); self.cb_role.addItems(["学生", "老师"])
        self.cb_role.currentTextChanged.connect(self._suggest_person_id)
        self.ed_pid = QLineEdit(); self.ed_pid.setPlaceholderText("人员 ID，如 S001 / T001")
        form.addRow("姓名：", self.ed_name)
        form.addRow("角色：", self.cb_role)
        form.addRow("人员 ID：", self.ed_pid)
        lay.addWidget(gb_info)

        # 录音
        gb_rec = QGroupBox("2. 录制声纹样本（有效语音 3~30 秒，推荐 10 秒左右）")
        v = QVBoxLayout(gb_rec)
        lbl_guide = QLabel(GUIDE_TEXT)
        lbl_guide.setStyleSheet("color:#444; background:#f5f7fa; padding:8px; "
                                "border:1px solid #ddd; border-radius:4px;")
        lbl_guide.setWordWrap(True)
        v.addWidget(lbl_guide)

        row = QHBoxLayout()
        self.btn_start = QPushButton("● 开始录音")
        self.btn_start.setStyleSheet("background:#2e7d32;color:white;padding:8px;")
        self.btn_stop = QPushButton("■ 停止录音")
        self.btn_stop.setStyleSheet("background:#c62828;color:white;padding:8px;")
        self.btn_stop.setEnabled(False)
        self.lbl_time = QLabel("00:00 / 00:30")
        self.lbl_time.setStyleSheet("font-size:16px;font-weight:bold;")
        row.addWidget(self.btn_start); row.addWidget(self.btn_stop)
        row.addStretch(1); row.addWidget(self.lbl_time)
        v.addLayout(row)

        self.bar = QProgressBar(); self.bar.setRange(0, MAX_SEC * 10)
        self.bar.setValue(0); self.bar.setTextVisible(False)
        v.addWidget(self.bar)

        self.lbl_quality = QLabel("尚未录音")
        self.lbl_quality.setStyleSheet("color:#666;")
        v.addWidget(self.lbl_quality)

        row2 = QHBoxLayout()
        self.btn_play = QPushButton("▶ 试听"); self.btn_play.setEnabled(False)
        self.btn_enroll = QPushButton("✔ 注册声纹")
        self.btn_enroll.setStyleSheet("background:#1565c0;color:white;padding:8px;"
                                      "font-weight:bold;")
        self.btn_enroll.setEnabled(False)
        self.btn_cancel_rr = QPushButton("✕ 取消重录")
        self.btn_cancel_rr.setVisible(False)
        self.btn_cancel_rr.clicked.connect(self._exit_rerecord)
        row2.addWidget(self.btn_play); row2.addWidget(self.btn_cancel_rr)
        row2.addStretch(1); row2.addWidget(self.btn_enroll)
        v.addLayout(row2)

        self.chk_local = QCheckBox(
            "同时把这段录音写进本地声纹库（近场录音，没有课堂片段时才勾）")
        self.chk_local.setVisible(False)
        v.addWidget(self.chk_local)
        self.lbl_domain = QLabel(DOMAIN_WARN)
        self.lbl_domain.setWordWrap(True)
        self.lbl_domain.setVisible(False)
        self.lbl_domain.setStyleSheet("color:#b26a00;")
        v.addWidget(self.lbl_domain)
        lay.addWidget(gb_rec)

        # 注册库
        gb_reg = QGroupBox("3. 已注册人员")
        v3 = QVBoxLayout(gb_reg)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["姓名", "角色", "人员 ID", "声纹 ID", "本地库", "留存样本"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        v3.addWidget(self.table)
        btn_row = QHBoxLayout()
        btn_edit = QPushButton("✎ 编辑 / 重新录制")
        btn_edit.setToolTip("改资料，或用「重新录制声纹」换掉这个人的声音")
        btn_edit.clicked.connect(self.edit_selected)
        self.btn_supplement = QPushButton("⬇ 补录到本地库")
        self.btn_supplement.setToolTip(
            "把课堂片段（远场）追加进本地库，用于录音中的离线识别")
        self.btn_supplement.clicked.connect(self.supplement_selected)
        self.btn_fill_all = QPushButton("⚡ 补全所有缺失")
        self.btn_fill_all.setToolTip("对本地库缺失的人，用留存样本批量补录")
        self.btn_fill_all.clicked.connect(self.supplement_all_missing)
        btn_refresh = QPushButton("刷新列表")
        btn_refresh.clicked.connect(self._refresh_registry)
        btn_row.addWidget(btn_edit)
        btn_row.addWidget(self.btn_supplement)
        btn_row.addWidget(self.btn_fill_all)
        btn_row.addStretch(1); btn_row.addWidget(btn_refresh)
        v3.addLayout(btn_row)
        self.lbl_sync = QLabel("")
        self.lbl_sync.setWordWrap(True)
        v3.addWidget(self.lbl_sync)
        lay.addWidget(gb_reg)

        # 日志
        self.txt_log = QPlainTextEdit(); self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumHeight(110)
        lay.addWidget(self.txt_log)

        # 信号
        self.btn_start.clicked.connect(self.start_record)
        self.btn_stop.clicked.connect(self.stop_record)
        self.btn_play.clicked.connect(self.play_wav)
        self.btn_enroll.clicked.connect(self.enroll)
        self.table.itemDoubleClicked.connect(lambda _item: self.edit_selected())

    # ---------------- 录音 ----------------
    def start_record(self):
        if self.proc:
            return
        self.wav_path = TMP_DIR / f"enroll_{time.strftime('%Y%m%d_%H%M%S')}.wav"
        log_file = TMP_DIR / "ffmpeg_enroll.log"
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-f", self.audio_fmt, "-i", self.audio_device,
               "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
               str(self.wav_path)]
        try:
            self._ffmpeg_log = open(log_file, "wb")
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=self._ffmpeg_log,
                creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception as exc:  # noqa: BLE001
            self.log(f"启动录音失败：{exc}")
            self.proc = None
            return
        self.record_start = time.time()
        self.timer.start()
        self.btn_start.setEnabled(False); self.btn_stop.setEnabled(True)
        self.btn_play.setEnabled(False); self.btn_enroll.setEnabled(False)
        self.lbl_quality.setText("录音中…… 请开始朗读")
        self.lbl_quality.setStyleSheet("color:#c62828;font-weight:bold;")
        self.log("开始录音")

    def stop_record(self):
        if not self.proc:
            return
        proc, self.proc = self.proc, None
        try:
            if proc.stdin:
                proc.stdin.write(b"q"); proc.stdin.flush()
            proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            proc.kill(); proc.wait(timeout=5)
        try:
            self._ffmpeg_log.close()
        except Exception:  # noqa: BLE001
            pass
        self.timer.stop()
        self.btn_start.setEnabled(True); self.btn_stop.setEnabled(False)
        self._check_recording()

    def _tick(self):
        if not self.proc:
            return
        if self.proc.poll() is not None:  # ffmpeg 自行退出（设备被占用等）
            self.proc = None
            self.timer.stop()
            self.btn_start.setEnabled(True); self.btn_stop.setEnabled(False)
            tail = ""
            try:
                tail = (TMP_DIR / "ffmpeg_enroll.log").read_text(
                    encoding="utf-8", errors="ignore")[-300:]
            except Exception:  # noqa: BLE001
                pass
            self.log(f"录音进程异常退出：{tail.strip() or '无错误输出'}")
            self.lbl_quality.setText("录音失败，请检查麦克风是否被占用")
            self.lbl_quality.setStyleSheet("color:#c62828;")
            return
        elapsed = time.time() - self.record_start
        self.lbl_time.setText(
            f"{int(elapsed)//60:02d}:{int(elapsed)%60:02d} / 00:{MAX_SEC}")
        self.bar.setValue(int(elapsed * 10))
        if elapsed >= MAX_SEC:
            self.log("达到 30 秒上限，自动停止")
            self.stop_record()

    # ---------------- 质检 ----------------
    def _check_recording(self):
        wav = self.wav_path
        if not wav or not wav.exists() or wav.stat().st_size < 1000:
            self.lbl_quality.setText("录音文件无效，请重录")
            self.lbl_quality.setStyleSheet("color:#c62828;")
            self.log("录音文件无效")
            return
        with wave.open(str(wav), "rb") as wf:
            dur = wf.getnframes() / wf.getframerate()
        # 音量检测
        out = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-i", str(wav),
             "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8",
            errors="ignore").stderr
        m = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", out)
        max_db = float(m.group(1)) if m else -99.0

        if dur < MIN_SEC:
            self.lbl_quality.setText(f"时长 {dur:.1f}s < {MIN_SEC}s，太短，请重录")
            self.lbl_quality.setStyleSheet("color:#c62828;")
            self.log(f"录音太短（{dur:.1f}s）")
            return
        if max_db < -35:
            self.lbl_quality.setText(
                f"时长 {dur:.1f}s，但音量过低（{max_db:.1f} dB），可能没录到声音，建议重录")
            self.lbl_quality.setStyleSheet("color:#e65100;")
            self.btn_play.setEnabled(True)
            self.log(f"音量过低：{max_db:.1f} dB")
            return
        self.lbl_quality.setText(
            f"样本就绪：时长 {dur:.1f}s，最大音量 {max_db:.1f} dB，可以试听或注册")
        self.lbl_quality.setStyleSheet("color:#2e7d32;font-weight:bold;")
        self.btn_play.setEnabled(True); self.btn_enroll.setEnabled(True)
        self.log(f"录音完成：{dur:.1f}s / {max_db:.1f} dB")

    def play_wav(self):
        if self.wav_path and self.wav_path.exists():
            winsound.PlaySound(str(self.wav_path),
                               winsound.SND_FILENAME | winsound.SND_ASYNC)

    # ---------------- 注册 ----------------
    def enroll(self):
        name = self.ed_name.text().strip()
        pid = self.ed_pid.text().strip()
        if not name:
            QMessageBox.warning(self, "缺少信息", "请先填写姓名")
            return
        if not pid:
            QMessageBox.warning(self, "缺少信息", "请先填写人员 ID")
            return
        if not self.wav_path or not self.wav_path.exists():
            QMessageBox.warning(self, "缺少录音", "请先录制声纹样本")
            return
        if len(name.encode("utf-8")) > 32:
            QMessageBox.warning(self, "姓名过长", "姓名 UTF-8 编码需 ≤ 32 字节")
            return

        if self.mode == "rerecord" and self.target:
            self._start_reenroll()
            return

        role = "student" if self.cb_role.currentText() == "学生" else "teacher"
        self.btn_enroll.setEnabled(False)
        self.btn_enroll.setText("注册中……")
        self.log(f"正在注册：{name}（{role}，{pid}）")
        self.worker = EnrollWorker(self.cloud_cfg, self.wav_path, pid, name, role)
        self.worker.finished_enroll.connect(self._on_enroll_done)
        self.worker.start()

    def _on_enroll_done(self, ok: bool, msg: str, _vpid: str):
        self.btn_enroll.setText("✔ 注册声纹")
        self.btn_enroll.setEnabled(True)
        self.log(msg.replace("\n", " | "))
        if not ok:
            QMessageBox.critical(self, "注册失败", msg)
            return
        pid = getattr(self.worker, "person_id", "")
        self._refresh_registry()
        tail = ""
        if pid and pid not in self.local_ids:
            tail = ("\n\n注意：此人目前只在云端库 —— 本地库（录音中离线识别用）还没有他。\n"
                    "要补上的话，在列表里选中他点「⬇ 补录到本地库」，"
                    "优先挑一段课堂片段（远场）。")
        QMessageBox.information(self, "注册成功", msg + tail)
        self._reset_after_save()

    # ---------------- 编辑已注册人员 ----------------
    def edit_selected(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "未选择", "请先在列表中选中一行（或双击该行）")
            return
        vpid_item = self.table.item(row, 3)
        if not vpid_item:
            return
        vpid = vpid_item.text()
        registry = self._load_registry()
        info = registry.get(vpid)
        if info is None:
            QMessageBox.warning(self, "数据不一致", "注册库中找不到该声纹 ID，请刷新列表")
            return

        loc = self.local_ids.get(str(info.get("person_id", "")))
        dlg = EditPersonDialog(self, info, vpid=vpid,
                               local_n=(loc or {}).get("n_samples", 0))
        code = dlg.exec()
        if code == EditPersonDialog.RERECORD:
            self._enter_rerecord(vpid, info)
            return
        if code != QDialog.Accepted:
            return
        new = dlg.values()
        if not new["person_name"]:
            QMessageBox.warning(self, "缺少信息", "姓名不能为空")
            return
        if not new["person_id"]:
            QMessageBox.warning(self, "缺少信息", "人员 ID 不能为空")
            return
        if len(new["person_name"].encode("utf-8")) > 32:
            QMessageBox.warning(self, "姓名过长", "姓名 UTF-8 编码需 ≤ 32 字节")
            return
        # 人员 ID 不能与其他人冲突
        for other_vpid, other in registry.items():
            if other_vpid != vpid and other.get("person_id") == new["person_id"]:
                QMessageBox.warning(self, "ID 冲突",
                                    f"人员 ID {new['person_id']} 已被 "
                                    f"{other.get('person_name', '')} 使用")
                return

        old_name = info.get("person_name", "")
        old_pid = info.get("person_id", "")
        registry[vpid] = new
        self.registry_file.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log(f"注册库已更新：{old_name}({old_pid}) → "
                 f"{new['person_name']}({new['person_id']}，{new['role']})")

        # 同步重命名留存样本文件
        if SAMPLE_DIR.exists():
            old_sample = SAMPLE_DIR / f"{old_pid}_{old_name}.wav"
            new_sample = SAMPLE_DIR / f"{new['person_id']}_{new['person_name']}.wav"
            if old_sample.exists() and old_sample != new_sample:
                try:
                    old_sample.rename(new_sample)
                    self.log(f"样本文件已重命名：{new_sample.name}")
                except OSError as exc:
                    self.log(f"样本重命名失败（不影响注册信息）：{exc}")

        self._refresh_registry()

        # 姓名变了才需要同步云端昵称（报告中的姓名以本地注册库为准）
        if new["person_name"] != old_name:
            self.nick_worker = UpdateNickWorker(
                self.cloud_cfg, vpid, new["person_name"])
            self.nick_worker.finished_update.connect(self._on_nick_done)
            self.nick_worker.start()

    # ---------------- 重新录制声纹 ----------------
    def _enter_rerecord(self, vpid: str, info: dict) -> None:
        """进入重录模式：锁死身份信息，把「注册」按钮换成「保存新声纹」。"""
        if self.proc:
            QMessageBox.warning(self, "正在录音", "请先停止当前录音，再切换模式。")
            return
        name = info.get("person_name", "")
        pid = info.get("person_id", "")
        role = info.get("role", "student")
        self.mode = "rerecord"
        self.target = {"vpid": vpid, "person_id": pid,
                       "person_name": name, "role": role}
        # 先改模式再动控件 —— setCurrentText 会触发 _suggest_person_id，
        # 而它见到 mode=="rerecord" 会直接返回，才不会把人员 ID 冲掉。
        self.cb_role.setCurrentText("老师" if role == "teacher" else "学生")
        self.ed_pid.setText(pid)
        self.ed_name.setText(name)
        for w in (self.ed_name, self.ed_pid, self.cb_role):
            w.setEnabled(False)
        self.banner.setText(
            f"🎙 重录模式：为「{name}（{pid}）」重新录制声纹\n"
            f"保存时云端原地更新 —— 声纹 ID {str(vpid)[:8]}… 保持不变，"
            f"不会在云端多出一条重复记录；enroll_samples 里的留存样本会被覆盖。\n"
            f"想放弃请点「✕ 取消重录」。")
        self.banner.setVisible(True)
        self.btn_cancel_rr.setVisible(True)
        self.btn_start.setText("● 开始重录")
        self.btn_enroll.setText("✔ 保存新声纹（覆盖旧的）")
        self.chk_local.setVisible(True)
        self.lbl_domain.setVisible(True)
        self.lbl_quality.setText("重录模式：请按平时上课的音量朗读")
        self.lbl_quality.setStyleSheet("color:#1565c0;")
        self.log(f"进入重录模式：{name}（{pid} / {vpid}）")

    def _exit_rerecord(self) -> None:
        """退出重录模式并复位表单（幂等：本来就不在重录模式时只做复位）。"""
        was = (self.mode == "rerecord")
        self.mode = "new"
        self.target = None
        self.ed_name.setEnabled(True)
        self.ed_pid.setEnabled(True)
        self.cb_role.setEnabled(True)
        self.banner.setVisible(False)
        self.btn_cancel_rr.setVisible(False)
        self.btn_start.setText("● 开始录音")
        self.btn_enroll.setText("✔ 注册声纹")
        self.chk_local.setChecked(False)      # 先取消勾选，再隐藏
        self.chk_local.setVisible(False)
        self.lbl_domain.setVisible(False)
        self.ed_name.clear()
        self._suggest_person_id()
        if was:
            self.log("已退出重录模式，回到「新注册」")

    def _on_local_check(self, on: bool) -> None:
        """勾「同时写本地库」时把域风险再确认一次。

        本地 CAM++ 认的是「声学域」：近场耳麦注册、远场课堂比对，
        同人余弦会从 0.8 掉到 0.3。随手勾上会把已经建好的本地库慢慢稀释掉，
        所以这里拦一道。
        """
        if not on:
            return
        if QMessageBox.warning(
                self, "确认写入本地库", DOMAIN_WARN + "\n\n确定要勾选吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            self.chk_local.setChecked(False)

    def _start_reenroll(self) -> None:
        t = self.target or {}
        self.btn_enroll.setEnabled(False)
        self.btn_enroll.setText("保存中……")
        self.log(f"正在重录：{t.get('person_name')}（{t.get('person_id')}）　"
                 f"同时写本地库={self.chk_local.isChecked()}")
        self.rerecord_worker = ReenrollWorker(
            self.cloud_cfg, self.wav_path, t["vpid"], t["person_id"],
            t["person_name"], t["role"], self.chk_local.isChecked())
        self.rerecord_worker.finished_reenroll.connect(self._on_reenroll_done)
        self.rerecord_worker.start()

    def _on_reenroll_done(self, ok: bool, msg: str) -> None:
        self.btn_enroll.setText("✔ 保存新声纹（覆盖旧的）")
        self.btn_enroll.setEnabled(True)
        for line in msg.splitlines():
            self.log(line)
        if ok:
            QMessageBox.information(self, "重录完成", msg)
            self._refresh_registry()
            self._reset_after_save()
        else:
            QMessageBox.critical(self, "重录失败", msg)

    def _reset_after_save(self) -> None:
        """保存成功后复位录音区（新注册与重录共用）。"""
        self._exit_rerecord()
        self.wav_path = None
        self.btn_play.setEnabled(False)
        self.btn_enroll.setEnabled(False)
        self.lbl_quality.setText("尚未录音")
        self.lbl_quality.setStyleSheet("color:#666;")
        self.bar.setValue(0)
        self.lbl_time.setText(f"00:00 / 00:{MAX_SEC}")

    # ---------------- 补录到本地库（只动本地，不碰云端） ----------------
    def _selected_person(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "未选择", "请先在列表中选中一行（或双击该行）")
            return None
        def cell(c: int) -> str:
            it = self.table.item(row, c)
            return it.text().strip() if it else ""
        return {"person_id": cell(2), "person_name": cell(0),
                "role": "teacher" if cell(1) == "老师" else "student"}

    @staticmethod
    def _clip_start_dir(name: str) -> Path:
        """文件选择框的起始目录：优先这个人的课堂片段目录。

        cloud_work 下的目录名就是「学生姓名-课程」，里面的 vp_*.wav 是
        从课堂录音里按音色切出来的**远场**片段 —— 正是本地库要的同域素材。
        没有课堂片段时才退回 enroll_samples（近场，效果差一截）。
        """
        dirs = sorted(d for d in CLASSROOM_DIR.glob(f"{name}-*")
                      if d.is_dir() and any(d.glob("vp_*.wav")))
        if dirs:
            return dirs[0]
        return SAMPLE_DIR if SAMPLE_DIR.exists() else CLASSROOM_DIR

    def supplement_selected(self) -> None:
        """给选中的人补录本地库 —— 素材由你挑，优先课堂远场片段。"""
        person = self._selected_person()
        if not person:
            return
        name = person["person_name"]
        files, _ = QFileDialog.getOpenFileNames(
            self, f"选择用于补录「{name}」的音频（优先课堂远场片段）",
            str(self._clip_start_dir(name)),
            "音频文件 (*.wav)")
        if not files:
            return
        near = [f for f in files if Path(f).parent == SAMPLE_DIR]
        if near and QMessageBox.warning(
                self, "近场样本提示",
                "你选中了 enroll_samples 里的近场录音。\n\n" + DOMAIN_WARN
                + "\n\n" + DOMAIN_EVIDENCE + "\n\n仍要用它补录吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        self._run_local_fill([{**person, "wav_paths": files}])

    def supplement_all_missing(self) -> None:
        """一键把所有「只在云端」的人用留存样本补进本地库。"""
        if not self.missing:
            QMessageBox.information(self, "无需补录", "本地库已覆盖云端库全部人员。")
            return
        usable = [m for m in self.missing if m["has_sample"]]
        no_sample = [m for m in self.missing if not m["has_sample"]]
        if not usable:
            QMessageBox.warning(
                self, "没有可用素材",
                f"有 {len(self.missing)} 人本地库缺失，但 enroll_samples 里没有他们的"
                "留存样本：\n　"
                + "、".join(m["person_name"] for m in no_sample)
                + "\n\n请在列表里选中某人点「⬇ 补录到本地库」，手动挑一段课堂片段。")
            return
        names = "、".join(m["person_name"] + "（" + m["person_id"] + "）"
                         for m in usable)
        extra = ("\n\n没有留存样本、本次跳过："
                 + "、".join(m["person_name"] for m in no_sample)) if no_sample else ""
        if QMessageBox.warning(
                self, "批量补录本地库（用留存样本）",
                f"为以下 {len(usable)} 人补录本地库：\n　{names}\n\n"
                + DOMAIN_WARN + "\n\n" + DOMAIN_EVIDENCE + extra
                + "\n\n更稳的做法：取消本框，在列表里逐个人点「⬇ 补录到本地库」，"
                  "挑他的课堂片段。\n仍要用留存样本一键补齐吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        self._run_local_fill([{**m, "wav_paths": [m["sample"]]} for m in usable])

    def _run_local_fill(self, jobs: list[dict]) -> None:
        if self.local_worker and self.local_worker.isRunning():
            QMessageBox.information(self, "正在进行", "上一批本地库写入还没结束，稍等。")
            return
        self.btn_supplement.setEnabled(False)
        self.btn_fill_all.setEnabled(False)
        self.log(f"开始写入本地库：{len(jobs)} 人")
        self.local_worker = LocalEnrollWorker(self.cloud_cfg, jobs)
        self.local_worker.finished_local.connect(self._on_local_filled)
        self.local_worker.start()

    def _on_local_filled(self, ok: bool, msg: str) -> None:
        self.btn_supplement.setEnabled(True)
        self.btn_fill_all.setEnabled(True)
        for line in msg.splitlines():
            self.log(line)
        if ok:
            QMessageBox.information(self, "本地库已更新", msg)
        else:
            QMessageBox.warning(self, "本地库更新失败", msg)
        self._refresh_registry()

    def _on_nick_done(self, ok: bool, msg: str):
        self.log(msg)
        if not ok:
            QMessageBox.warning(self, "云端昵称同步失败", msg)

    # ---------------- 注册库 ----------------
    def _load_registry(self) -> dict:
        if self.registry_file.exists():
            try:
                return load_json(self.registry_file)
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def _refresh_registry(self):
        """重画列表，并顺手算一遍「云端有、本地没有」的差异。

        这个差异是真实存在的老问题：EnrollWorker 走的是腾讯云注册 + 写
        voiceprint_registry.json，**从不写本地库**（本地库的向量空间与腾讯不互通，
        必须另算一道 CAM++）。所以经本工具注册的人默认只有云端记录。
        """
        reg = self._load_registry()
        self.local_ids = read_local_persons(self.cloud_cfg)
        rows = sorted(reg.items(), key=lambda kv: kv[1].get("person_id", ""))
        self.table.setRowCount(len(rows))
        self.missing = []
        for row, (vpid, info) in enumerate(rows):
            pid = str(info.get("person_id", ""))
            name = info.get("person_name", "")
            role_cn = {"teacher": "老师", "student": "学生"}.get(
                info.get("role", ""), info.get("role", ""))
            loc = self.local_ids.get(pid)
            sample = SAMPLE_DIR / f"{pid}_{name}.wav"
            vals = [name, role_cn, pid, vpid,
                    f"✓ {loc['n_samples']} 段" if loc else "✗ 缺",
                    "有" if sample.exists() else "—"]
            for col, val in enumerate(vals):
                item = QTableWidgetItem(str(val))
                if col == 4:
                    item.setForeground(QBrush(QColor(
                        "#2e7d32" if loc else "#c62828")))
                self.table.setItem(row, col, item)
            if not loc:
                self.missing.append({
                    "person_id": pid, "person_name": name,
                    "role": info.get("role", ""), "vpid": vpid,
                    "sample": sample, "has_sample": sample.exists()})

        n_miss = len(self.missing)
        if not n_miss:
            self.lbl_sync.setText(
                f"云端库 {len(rows)} 人，本地库已全覆盖，无需补录。")
            self.lbl_sync.setStyleSheet("color:#2e7d32;")
        else:
            with_sample = sum(1 for m in self.missing if m["has_sample"])
            teachers = [m for m in self.missing if m["role"] == "teacher"]
            self.lbl_sync.setText(
                f"⚠ 云端库 {len(rows)} 人，其中 {n_miss} 人本地库缺失（✗）："
                + "、".join(f"{m['person_name']}({m['person_id']})"
                            for m in self.missing)
                + f"\n本地库用于录音中识别，缺失者认不出；有留存样本可一键补的 {with_sample} 人。"
                + ("\n其中教师缺失最要命：认不出教师 → VAD 课无法归课 → 仅存档。"
                   if teachers else ""))
            self.lbl_sync.setStyleSheet("color:#c62828;")

    def _suggest_person_id(self):
        if self.mode == "rerecord":
            return                      # 重录时人员 ID 是锚点，不许自动改
        prefix = "S" if self.cb_role.currentText() == "学生" else "T"
        used = set()
        for info in self._load_registry().values():
            pid = str(info.get("person_id", ""))
            m = re.fullmatch(rf"{prefix}(\d+)", pid)
            if m:
                used.add(int(m.group(1)))
        nxt = 1
        while nxt in used:
            nxt += 1
        self.ed_pid.setText(f"{prefix}{nxt:03d}")

    # ---------------- 日志 ----------------
    def log(self, msg: str):
        self.txt_log.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def closeEvent(self, event):  # noqa: N802
        if self.proc:
            self.stop_record()
        winsound.PlaySound(None, winsound.SND_PURGE)
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        main()
    except Exception:  # pythonw 无控制台，启动失败必须留痕
        import traceback
        from datetime import datetime as _dt
        with open(r"C:\recorder\voiceprint_gui_error.log", "a", encoding="utf-8") as f:
            f.write(f"\n===== {_dt.now().isoformat(timespec='seconds')} =====\n")
            f.write(traceback.format_exc())
        raise
