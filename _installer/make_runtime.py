# -*- coding: utf-8 -*-
"""构建精简独立运行时到 build/stage/venv（安装包的核心）。

设计要点（均有实测依据，勿轻易改）：

1. **铺成 venv 布局**：把完整 Python 3.13 安装铺到 stage\\venv\\，
   console.py 里写死的 venv\\Scripts\\pythonw.exe 因此『零改动可用』。
2. **Scripts 里放真实解释器副本**（不是 venv 那种 241KB 重定向桩），
   并把 python313.dll 同置其中 —— 实测真实解释器能向上 landmark 到
   venv\\Lib，sys.prefix 正确解析为 venv 根，stdlib 可正常导入。
3. **不得残留 pyvenv.cfg**：有它就会被当成 venv 去读基础解释器，换机器即崩。
4. **瘦身一律在『复制时排除』，不做『复制后再删』**：既避开批量删除限制，
   也少写一遍 780MB。排除白名单的依据见 PYSIDE6_DROP_* 注释。

用法：<出口python> make_runtime.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

# ------------------------------------------------------------------ 路径常量
INSTALLER = Path(r"C:\recorder\_installer")
BUILD = INSTALLER / "build"
STAGE = BUILD / "stage"
TARGET_VENV = STAGE / "venv"
TARGET_SP = TARGET_VENV / "Lib" / "site-packages"

PY_SRC = Path.home() / ".workbuddy" / "binaries" / "python" / "versions" / "3.13.12"
VENV_SP = Path(r"C:\recorder\venv\Lib\site-packages")
SYS32 = Path(r"C:\Windows\System32")

MB = 1048576


# ------------------------------------------------------------------ 小工具
def say(msg: str) -> None:
    print(msg, flush=True)


def dir_size(p: Path) -> float:
    t = 0
    for r, _d, f in os.walk(p):
        for n in f:
            try:
                t += os.path.getsize(os.path.join(r, n))
            except OSError:
                pass
    return t / MB


def robocopy(src: Path, dst: Path, xd: list | None = None,
             xf: list | None = None, label: str = "") -> None:
    """批量复制。返回码 0-7 视为成功（robocopy 用位标志表达结果）。"""
    dst.mkdir(parents=True, exist_ok=True)
    cmd = ["robocopy", str(src), str(dst), "/E", "/MT:16", "/NFL", "/NDL",
           "/NJH", "/NJS", "/NP", "/R:1", "/W:1"]
    if xd:
        cmd.append("/XD")
        cmd += [str(p) for p in xd]
    if xf:
        cmd.append("/XF")
        cmd += [str(p) for p in xf]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode >= 8:
        raise RuntimeError(f"robocopy {label} 失败 rc={r.returncode}\n{(r.stdout or '')[-1500:]}")


def wipe(p: Path) -> None:
    """整目录删除。用 cmd 的 rd /s /q —— 实测比 robocopy 镜像快一个数量级
    （robocopy 单线程删 5 万个小文件要 7 分钟以上，rd 只需十几秒）。"""
    if not p.exists():
        return
    subprocess.run(["cmd", "/c", "rd", "/s", "/q", str(p)],
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace",
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if p.exists():
        # 极少数被占用删不掉的情况，退化为 robocopy 镜像 + 兜底
        empty = BUILD / "_empty"
        empty.mkdir(parents=True, exist_ok=True)
        subprocess.run(["robocopy", str(empty), str(p), "/MIR", "/NFL", "/NDL",
                        "/NJH", "/NJS", "/NP", "/R:0", "/W:0"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
        shutil.rmtree(p, ignore_errors=True)


# ------------------------------------------------------------------ 1) Python 运行时
def build_python() -> None:
    say("\n[1/5] 复制 Python 解释器与标准库 …")
    py = PY_SRC
    if not (py / "python.exe").exists():
        raise SystemExit(f"找不到源解释器：{py}\\python.exe")

    xd = [py / "include", py / "libs", py / "Scripts", py / "tcl",
          py / "Lib" / "site-packages", py / "Lib" / "test",
          py / "Lib" / "idlelib", py / "Lib" / "tkinter",
          py / "Lib" / "turtledemo", py / "Lib" / "ensurepip",
          py / "Lib" / "lib2to3", py / "Lib" / "distutils",
          "__pycache__"]
    robocopy(py, TARGET_VENV, xd=xd, xf=["*.pyc", "*.pdb"], label="python")
    say(f"    venv 根：{dir_size(TARGET_VENV):.1f} MB")


# ------------------------------------------------------------------ 2) Scripts 解释器
def build_scripts() -> None:
    say("\n[2/5] 在 Scripts\\ 放置真实解释器（console.py 依赖此路径）…")
    scripts = TARGET_VENV / "Scripts"
    if scripts.exists():
        wipe(scripts)
    scripts.mkdir(parents=True, exist_ok=True)

    # 真实解释器 + 加载时必需的 DLL（缺 python313.dll 会直接起不来）
    for n in ("python.exe", "pythonw.exe", "python313.dll", "python3.dll",
              "vcruntime140.dll", "vcruntime140_1.dll",
              "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "concrt140.dll"):
        s = (PY_SRC / n) if (PY_SRC / n).exists() else (SYS32 / n)
        if s.exists():
            shutil.copy2(s, scripts / n)
    say(f"    Scripts：{len(list(scripts.iterdir()))} 个文件")

    # 根目录也补一份 MSVC 运行库（app-local，避免依赖目标机装过 VC++）
    for n in ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
              "concrt140.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        s = SYS32 / n
        if s.exists():
            shutil.copy2(s, TARGET_VENV / n)


# ------------------------------------------------------------------ 3) 第三方包
# PySide6 可删项的依据：本项目只 import QtCore / QtGui / QtWidgets
# （对 82 个源文件做 AST 全量扫描确认，无 QtNetwork / QtSvg 之外的模块）
PYSIDE6_DROP_PREFIX = (
    "Qt6WebEngine", "QtWebEngine", "Qt6Pdf", "QtPdf",
    "Qt63D", "Qt3D", "Qt6Charts", "QtCharts",
    "Qt6DataVisualization", "QtDataVisualization", "Qt6Graphs", "QtGraphs",
    "Qt6Multimedia", "QtMultimedia", "Qt6Sql", "QtSql",
    "Qt6Test", "QtTest", "Qt6Bluetooth", "QtBluetooth", "Qt6Nfc", "QtNfc",
    "Qt6Positioning", "QtPositioning", "Qt6Sensors", "QtSensors",
    "Qt6SerialPort", "QtSerialPort", "Qt6WebSockets", "QtWebSockets",
    "Qt6WebChannel", "QtWebChannel", "Qt6RemoteObjects", "QtRemoteObjects",
    "Qt6Scxml", "QtScxml", "Qt6SpatialAudio", "QtSpatialAudio",
    "Qt6StateMachine", "QtStateMachine", "Qt6Help", "QtHelp",
    "Qt6UiTools", "QtUiTools", "Qt6Location", "QtLocation",
    "Qt6TextToSpeech", "QtTextToSpeech", "Qt6VirtualKeyboard", "QtVirtualKeyboard",
    "Qt6HttpServer", "QtHttpServer", "Qt6Labs", "QtLabs",
    "Qt6Quick", "QtQuick", "Qt6Qml", "QtQml",
    "Qt6Designer", "QtDesigner", "Qt6ShaderTools", "QtShaderTools",
    "Qt6Concurrent", "QtConcurrent", "QtNetworkAuth",
    "QtOpenGL", "QtOpenGLWidgets", "QtTest", "QtSql",
)
PYSIDE6_DROP_DIRS = ("qml", "translations", "metatypes", "typesystems",
                     "include", "doc", "scripts", "support", "lib", "glue")
PYSIDE6_DROP_PLUGINS = ("designer", "qmllint", "qmltooling", "sqldrivers",
                        "platforminputcontexts", "vectorimageformats")
# tencentcloud 实际只用到这两个产品模块
TENCENTCLOUD_KEEP = ("common", "asr")


def build_site_packages() -> None:
    say("\n[3/5] 复制 site-packages（复制时即排除，不做事后删除）…")
    if not VENV_SP.is_dir():
        raise SystemExit(f"找不到源 site-packages：{VENV_SP}")

    ps_src = VENV_SP / "PySide6"
    tc_src = VENV_SP / "tencentcloud"

    # 3.1 主体：排除三个大件与缓存/测试数据
    robocopy(VENV_SP, TARGET_SP,
             xd=[ps_src, tc_src,
                 VENV_SP / "pip", VENV_SP / "setuptools", VENV_SP / "wheel",
                 VENV_SP / "matplotlib" / "mpl-data" / "sample_data",
                 "__pycache__"],
             xf=["*.pyc", "*.pyo"],
             label="site-packages")
    say(f"    主体复制完成：{dir_size(TARGET_SP):.1f} MB")

    # 3.2 PySide6：按排除规则复制（这是最大的一块）
    if ps_src.is_dir():
        xd = [ps_src / d for d in PYSIDE6_DROP_DIRS]
        xd += [ps_src / "plugins" / d for d in PYSIDE6_DROP_PLUGINS]
        xd += ["__pycache__"]
        xf = [p + "*" for p in PYSIDE6_DROP_PREFIX] + ["*.pyi", "*.pyc", "*.pyo"]
        robocopy(ps_src, TARGET_SP / "PySide6", xd=xd, xf=xf, label="PySide6")
        say(f"    PySide6 {dir_size(ps_src):.1f} → {dir_size(TARGET_SP / 'PySide6'):.1f} MB")

    # 3.3 tencentcloud：只按白名单复制两个产品模块
    if tc_src.is_dir():
        tct = TARGET_SP / "tencentcloud"
        tct.mkdir(parents=True, exist_ok=True)
        init = tc_src / "__init__.py"
        if init.exists():
            shutil.copy2(init, tct / "__init__.py")
        for sub in TENCENTCLOUD_KEEP:
            if (tc_src / sub).is_dir():
                robocopy(tc_src / sub, tct / sub,
                         xd=["__pycache__"], xf=["*.pyc"], label=f"tencentcloud/{sub}")
        say(f"    tencentcloud {dir_size(tc_src):.1f} → {dir_size(tct):.1f} MB")

    say(f"    site-packages 合计：{dir_size(TARGET_SP):.1f} MB")


# ------------------------------------------------------------------ 4) 清洁检查
def verify() -> None:
    say("\n[4/5] 清洁检查 …")
    # pyvenv.cfg 绝不能存在：有它就会被当成 venv 去找基础解释器
    for p in (TARGET_VENV / "pyvenv.cfg", TARGET_VENV / "Scripts" / "pyvenv.cfg"):
        if p.exists():
            p.unlink()
            say(f"    已清除 {p.name}")

    missing = [rel for rel in
               ("python.exe", "pythonw.exe", "python313.dll",
                r"Scripts\python.exe", r"Scripts\pythonw.exe",
                r"Scripts\python313.dll", r"Lib\os.py",
                r"Lib\site-packages\PySide6\QtCore.pyd",
                r"Lib\site-packages\PySide6\plugins\platforms\qwindows.dll",
                r"Lib\site-packages\tencentcloud\asr\v20190614\asr_client.py",
                r"Lib\site-packages\onnxruntime\capi\onnxruntime.dll",
                r"Lib\site-packages\sherpa_onnx")
               if not (TARGET_VENV / rel).exists()]
    if missing:
        raise SystemExit("✗ 运行时缺少关键文件:\n  " + "\n  ".join(missing))
    say("    ✓ 关键文件齐备，无 pyvenv.cfg")


# ------------------------------------------------------------------ 5) 冒烟测试
SMOKE = r"""
import sys, json
mods = ["json","re","subprocess","threading","ctypes","sqlite3","ssl",
        "logging","datetime","pathlib","urllib.request","email","hashlib",
        "numpy","requests","qcloud_cos","openpyxl","sherpa_onnx",
        "onnxruntime","fpdf","PIL","matplotlib","Crypto"]
bad = []
for m in mods:
    try:
        __import__(m)
    except Exception as e:
        bad.append("%s -> %s: %s" % (m, type(e).__name__, e))
try:
    from PySide6.QtCore import Qt, QThread, QTimer, Signal
    from PySide6.QtGui import QIcon, QFont, QColor, QPainter
    from PySide6.QtWidgets import QApplication, QWidget, QLabel, QPushButton
except Exception as e:
    bad.append("PySide6 -> %s: %s" % (type(e).__name__, e))
try:
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.asr.v20190614 import asr_client, models
except Exception as e:
    bad.append("tencentcloud -> %s: %s" % (type(e).__name__, e))
print(json.dumps({"prefix": sys.prefix, "bad": bad}, ensure_ascii=False))
"""


def smoke() -> None:
    say("\n[5/5] 运行时自检（用 Scripts\\python.exe 导入全部第三方库）…")
    exe = TARGET_VENV / "Scripts" / "python.exe"
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    r = subprocess.run([str(exe), "-c", SMOKE], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env,
                       cwd=str(TARGET_VENV))
    lines = (r.stdout or "").strip().splitlines()
    if not lines:
        raise SystemExit(f"✗ 自检无输出\nstderr={r.stderr[-2000:]}")
    try:
        info = json.loads(lines[-1])
    except Exception:
        raise SystemExit(f"✗ 自检输出无法解析\nstdout={r.stdout[-1000:]}\n"
                         f"stderr={r.stderr[-1500:]}")
    say(f"    sys.prefix = {info['prefix']}")
    if info["bad"]:
        say("    ✗ 以下模块导入失败：")
        for b in info["bad"]:
            say(f"      - {b}")
        raise SystemExit(1)
    say("    ✓ 全部模块导入成功")


if __name__ == "__main__":
    say(f"源解释器        : {PY_SRC}")
    say(f"源 site-packages: {VENV_SP}")
    say(f"目标            : {TARGET_VENV}")

    # 必须先把旧产物清空：robocopy 只做增量，残留会把瘦身结果「补」回来
    if TARGET_VENV.exists():
        say("清理旧产物 …")
        wipe(TARGET_VENV)

    build_python()
    build_scripts()
    build_site_packages()
    verify()
    smoke()

    say("\n" + "=" * 58)
    say(f"运行时总大小：{dir_size(TARGET_VENV):.1f} MB")
    say("=" * 58)
