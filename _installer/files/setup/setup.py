# -*- coding: utf-8 -*-
"""课堂录制自动教评系统 —— 安装后处理（安装 / 配置 / 自检 / 卸载）。

由 _setup 下的三个 .bat 调用：

    python setup.py install      建快捷方式 + 写卸载注册项
    python setup.py config       填写腾讯云与 LLM 密钥（GUI，失败降级控制台）
    python setup.py check        环境自检（ffmpeg / 模型 / Node / lark-cli / PDF 生成器）
    python setup.py uninstall    清理快捷方式与注册项

说明：安装目录固定为 C:\\recorder —— 系统代码里有大量以该路径为准的引用，
换目录会导致录制、上传、报告链路找不到文件，故此处不做可配置化。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"C:\recorder")
SETUP_DIR = ROOT / "_setup"
APP_NAME = "课堂录制自动教评系统"
UNINST_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ClassroomRecorder"

PY = ROOT / "venv" / "Scripts" / "python.exe"
PYW = ROOT / "venv" / "Scripts" / "pythonw.exe"
NODE = ROOT / "vendor" / "node" / "node.exe"
LARK = ROOT / "vendor" / "node" / "lark-cli" / "scripts" / "run.js"
FFMPEG = ROOT / "ffmpeg" / "bin" / "ffmpeg.exe"
PDFGEN = ROOT / "vendor" / "teaching_eval" / "generate_pdf.py"

# 入口脚本（桌面快捷方式指向它们；不存在则退回通用启动器）
ENTRIES = [
    ("课堂录制控制台", "启动课堂控制台.bat",
     "打开统一控制台（总览/录课/麦克风/提示词/云端/飞书）"),
    ("一键重启课堂系统", "一键重启课堂系统.bat",
     "正在录制时中止，避免把一节课截断"),
    ("强制重启课堂系统", "强制重启课堂系统.bat",
     "无条件重启（录制中也会停）"),
]


def say(msg: str = "") -> None:
    print(msg, flush=True)


def desktop_dir() -> Path:
    return Path(os.path.expanduser("~")) / "Desktop"


def start_menu_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path(os.path.expanduser("~")) / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP_NAME


# ------------------------------------------------------------------ 快捷方式
def make_shortcut(lnk: Path, target: Path, workdir: Path | None = None,
                  description: str = "") -> bool:
    """用 PowerShell 的 WScript.Shell 建 .lnk（全系统可用，无需额外依赖）。"""
    lnk.parent.mkdir(parents=True, exist_ok=True)
    ps = (
        "$ws = New-Object -ComObject WScript.Shell;"
        f"$s = $ws.CreateShortcut('{lnk}');"
        f"$s.TargetPath = '{target}';"
        f"$s.WorkingDirectory = '{workdir or target.parent}';"
        f"$s.Description = '{description}';"
        "$s.Save()"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-Command", ps],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        say(f"  ✗ 建快捷方式失败：{lnk.name}  {r.stderr.strip()[:160]}")
        return False
    return True


def install() -> None:
    say("=" * 62)
    say(f"  {APP_NAME} —— 安装后处理")
    say("=" * 62)

    ok = 0
    desk = desktop_dir()
    say(f"\n[1/3] 创建桌面快捷方式 → {desk}")
    for name, bat, desc in ENTRIES:
        target = ROOT / bat
        if not target.exists():
            say(f"  · 跳过 {name}（找不到 {bat}）")
            continue
        if make_shortcut(desk / f"{name}.lnk", target, ROOT, desc):
            say(f"  ✓ {name}")
            ok += 1

    say(f"\n[2/3] 创建开始菜单项")
    sm = start_menu_dir()
    for name, bat, desc in ENTRIES:
        target = ROOT / bat
        if target.exists() and make_shortcut(sm / f"{name}.lnk", target, ROOT, desc):
            say(f"  ✓ {name}")
    # 卸载入口
    un_bat = SETUP_DIR / "uninstall.bat"
    if un_bat.exists():
        make_shortcut(sm / "卸载.lnk", un_bat, SETUP_DIR, "卸载本系统")

    say(f"\n[3/3] 写入卸载注册项")
    ps = (
        f"$k = 'HKCU:\\{UNINST_KEY}';"
        "New-Item -Path $k -Force | Out-Null;"
        f"Set-ItemProperty -Path $k -Name DisplayName -Value '{APP_NAME}';"
        f"Set-ItemProperty -Path $k -Name InstallLocation -Value '{ROOT}';"
        f"Set-ItemProperty -Path $k -Name UninstallString -Value '\"{un_bat}\"';"
        "Set-ItemProperty -Path $k -Name DisplayVersion -Value '1.0';"
        "Set-ItemProperty -Path $k -Name Publisher -Value 'Kings Academy';"
        "Set-ItemProperty -Path $k -Name NoModify -Value 1 -Type DWord;"
        "Set-ItemProperty -Path $k -Name NoRepair -Value 1 -Type DWord"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-Command", ps],
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace",
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    say("  ✓ 已登记到「添加或删除程序」")

    say(f"\n完成：{ok} 个快捷方式已创建。")
    say("下一步请运行『首次配置.bat』填写密钥。")


# ------------------------------------------------------------------ 配置向导
FIELDS = [
    ("secret_id", "腾讯云 SecretId", "访问管理 → API 密钥管理"),
    ("secret_key", "腾讯云 SecretKey", "访问管理 → API 密钥管理"),
    ("llm_api_key", "大模型 API Key", "TokenHub / DeepSeek 的调用密钥"),
]


def _load_targets() -> list[Path]:
    return [ROOT / "cloud" / "cloud_config.json", ROOT / "recorder" / "config.json"]


def _current_values() -> dict:
    vals = {}
    try:
        cfg = json.loads((ROOT / "cloud" / "cloud_config.json").read_text(encoding="utf-8"))
        vals["secret_id"] = cfg.get("cos", {}).get("secret_id", "")
        vals["secret_key"] = cfg.get("cos", {}).get("secret_key", "")
        vals["llm_api_key"] = cfg.get("llm", {}).get("tokenhub_api_key", "")
    except Exception:
        pass
    return vals


def _is_placeholder(v: str) -> bool:
    return (not v) or v.upper().startswith("PUT-YOUR")


def _write_keys(new: dict) -> None:
    p = ROOT / "cloud" / "cloud_config.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg.setdefault("cos", {})["secret_id"] = new["secret_id"]
    cfg["cos"]["secret_key"] = new["secret_key"]
    cfg.setdefault("llm", {})["tokenhub_api_key"] = new["llm_api_key"]
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    p2 = ROOT / "recorder" / "config.json"
    cfg2 = json.loads(p2.read_text(encoding="utf-8"))
    cfg2.setdefault("cos", {})["secret_id"] = new["secret_id"]
    cfg2["cos"]["secret_key"] = new["secret_key"]
    p2.write_text(json.dumps(cfg2, ensure_ascii=False, indent=2), encoding="utf-8")


def _gui_collect(cur: dict) -> dict | None:
    """PySide6 输入框；不可用时返回 None 让调用方降级。"""
    try:
        from PySide6.QtWidgets import (QApplication, QDialog, QFormLayout,
                                       QLineEdit, QDialogButtonBox, QLabel,
                                       QVBoxLayout)
    except Exception:
        return None
    app = QApplication.instance() or QApplication(sys.argv)
    dlg = QDialog()
    dlg.setWindowTitle(f"{APP_NAME} · 首次配置")
    dlg.resize(560, 260)
    outer = QVBoxLayout(dlg)
    tip = QLabel("密钥写入本机配置文件，不会上传到任何第三方。\n"
                 "密钥在腾讯云「访问管理 → API 密钥管理」获取。")
    tip.setWordWrap(True)
    outer.addWidget(tip)
    form = QFormLayout()
    edits = {}
    for key, label, hint in FIELDS:
        e = QLineEdit(cur.get(key, ""))
        if _is_placeholder(cur.get(key, "")):
            e.setPlaceholderText(f"请粘贴{label}")
        e.setEchoMode(QLineEdit.EchoMode.Password if key != "secret_id"
                      else QLineEdit.EchoMode.Normal)
        e.setMinimumWidth(380)
        edits[key] = e
        form.addRow(f"{label}（{hint}）", e)
    outer.addLayout(form)
    box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                           | QDialogButtonBox.StandardButton.Cancel)
    box.accepted.connect(dlg.accept)
    box.rejected.connect(dlg.reject)
    outer.addWidget(box)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return {k: e.text().strip() for k, e in edits.items()}


def _console_collect(cur: dict) -> dict | None:
    say("\n请依次输入密钥（直接回车 = 保持原值）：")
    out = {}
    for key, label, hint in FIELDS:
        shown = "（已配置）" if not _is_placeholder(cur.get(key, "")) else "（未配置）"
        try:
            v = input(f"  {label} {shown} [{hint}]: ").strip()
        except EOFError:
            return None
        out[key] = v or cur.get(key, "")
    return out


def config() -> None:
    say("=" * 62)
    say(f"  {APP_NAME} —— 首次配置")
    say("=" * 62)
    cur = _current_values()
    new = _gui_collect(cur)
    if new is None:
        say("（图形界面不可用，改用控制台输入）")
        new = _console_collect(cur)
    if not new:
        say("已取消，未修改任何配置。")
        return

    missing = [lbl for k, lbl, _ in FIELDS if _is_placeholder(new.get(k, ""))
               and k != "llm_api_key"]
    if missing:
        say(f"\n⚠ 以下必填项仍为空：{'、'.join(missing)}")
        say("  配置已保存，但上传/转写会失败。可稍后重跑『首次配置.bat』。")
    _write_keys(new)
    say("\n✓ 密钥已写入：")
    say("    cloud/cloud_config.json")
    say("    recorder/config.json")
    check()
    say("\n⚠ 云端能力还需两步一次性准备：")
    say("    1) 飞书授权：双击 _setup\\飞书登录.bat 或控制台 →「飞书」页登录")
    say("    2) 授课教师：控制台 →「录课」页下拉栏选择（课表匹配依据，无需声纹）")


# ------------------------------------------------------------------ 自检
def check() -> None:
    say("\n" + "-" * 62)
    say("环境自检")
    say("-" * 62)
    items = [
        ("Python 运行时", PY, True),
        ("pythonw", PYW, True),
        ("ffmpeg", FFMPEG, True),
        ("PDF 生成器", PDFGEN, True),
        ("Node 运行时", NODE, False),
        ("lark-cli", LARK, False),
    ]
    for label, p, required in items:
        tag = "✓" if p.exists() else ("✗" if required else "·")
        extra = "" if p.exists() else ("  ← 缺失（必需）" if required else "  ← 缺失（飞书同步不可用）")
        say(f"  {tag} {label:<14} {extra}")

    try:
        cfg = json.loads((ROOT / "cloud" / "cloud_config.json").read_text(encoding="utf-8"))
        sid = cfg.get("cos", {}).get("secret_id", "")
        key = cfg.get("llm", {}).get("tokenhub_api_key", "")
        say(f"  {'✓' if not _is_placeholder(sid) else '✗'} 腾讯云密钥"
            f"{'' if not _is_placeholder(sid) else '      ← 仍是占位符'}")
        say(f"  {'✓' if not _is_placeholder(key) else '✗'} 大模型密钥"
            f"{'' if not _is_placeholder(key) else '      ← 仍是占位符（教评文本会失败）'}")
    except Exception as e:
        say(f"  ✗ 读取 cloud_config.json 失败：{e}")

    lark_home = Path(os.path.expanduser("~")) / ".lark-cli"
    has_cfg = (lark_home / "config.json").exists()
    say(f"  {'✓' if has_cfg else '·'} 飞书登录态   {lark_home}"
        f"{'' if has_cfg else '  ← 尚未登录'}")

    for d in ("status", "logs", "reports", "cloud_work"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------ 卸载
def uninstall() -> None:
    say("=" * 62)
    say(f"  {APP_NAME} —— 卸载")
    say("=" * 62)

    desk = desktop_dir()
    removed = 0
    for name, _bat, _d in ENTRIES:
        for base in (desk, start_menu_dir()):
            f = base / f"{name}.lnk"
            if f.exists():
                f.unlink()
                removed += 1
    sm = start_menu_dir()
    if sm.exists():
        try:
            sm.rmdir()
        except OSError:
            pass
    say(f"  ✓ 已删除 {removed} 个快捷方式")

    try:
        import winreg
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINST_KEY)
        say("  ✓ 已移除卸载注册项")
    except Exception as e:
        say(f"  · 注册项未移除（可能本就不存在）：{e}")

    say("")
    ans = input("是否同时删除录音目录 C:\\recordings ？（y/N）：").strip().lower()
    if ans == "y":
        import shutil
        shutil.rmtree(r"C:\recordings", ignore_errors=True)
        say("  ✓ 录音目录已删除")

    say("\n程序目录 C:\\recorder 需手动删除（本次不自动删，避免误删配置与报告）。")
    say("确认无误后，可在文件资源管理器里直接删除该文件夹。")


# ------------------------------------------------------------------ 入口
if __name__ == "__main__":
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "check").lower()
    if cmd == "install":
        install()
    elif cmd == "config":
        config()
    elif cmd == "uninstall":
        uninstall()
    elif cmd == "check":
        check()
    else:
        say(f"未知命令：{cmd}（可用：install / config / check / uninstall）")
        sys.exit(2)
    say("")
