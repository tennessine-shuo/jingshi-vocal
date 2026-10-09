# -*- coding: utf-8 -*-
"""组装安装载荷到 build/stage（= 目标机上的 C:\\recorder）。

重要：本脚本**只往 stage 里加东西，绝不清理 stage\\venv**
（那是 make_runtime.py 的产物）。其余子目录在复制前逐个清空，
避免上一轮的残留混进安装包。

密钥策略：cos.secret_id / cos.secret_key / llm.tokenhub_api_key
一律替换为占位符（发行版选择「可安全分发」）。装完后用『首次配置.bat』填入。
声纹库默认**不打包**（个人生物特征数据）；需要时把 INCLUDE_VOICEPRINT 置 True。

用法：<出口python> make_payload.py
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
STAGE = BUILD / "stage"              # 会变成 C:\recorder
FILES = INSTALLER / "files"
SRC = Path(r"C:\recorder")           # 开发机上的系统本体
DESKTOP = Path(os.path.expanduser("~")) / "Desktop"

_WB = Path.home() / ".workbuddy"
PY_NODE = _WB / "binaries" / "node" / "versions" / "22.22.2-6" / "node.exe"
LARK_SRC = (_WB / "binaries" / "node" / "cli-connector-packages"
            / "node_modules" / "@larksuite" / "cli")
PDF_GEN_SRC = _WB / "skills" / "teaching-eval" / "assets" / "generate_pdf.py"
FFMPEG_SRC = Path(r"C:\ffmpeg\bin\ffmpeg.exe")

# 是否把声纹库（个人生物特征数据）打进安装包
INCLUDE_VOICEPRINT = False

MB = 1048576

PLACEHOLDER = {
    "secret_id": "PUT-YOUR-TENCENT-SECRET-ID-HERE",
    "secret_key": "PUT-YOUR-TENCENT-SECRET-KEY-HERE",
    "api_key": "PUT-YOUR-LLM-API-KEY-HERE",
}

STEPS = ["复制应用代码", "捆绑 ffmpeg", "捆绑 Node / lark-cli / PDF 生成器",
         "建立运行时目录", "放入 _setup 安装后处理脚本", "替换密钥为占位符",
         "载荷清点"]
_N = [0]


def step(title: str) -> None:
    _N[0] += 1
    say(f"\n[{_N[0]}/{len(STEPS)}] {title} …")


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


def wipe(p: Path) -> None:
    """整目录删除。用 cmd 的 rd /s /q —— 比 robocopy 镜像快一个数量级。"""
    if not p.exists():
        return
    subprocess.run(["cmd", "/c", "rd", "/s", "/q", str(p)],
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace",
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if p.exists():
        empty = BUILD / "_empty"
        empty.mkdir(parents=True, exist_ok=True)
        subprocess.run(["robocopy", str(empty), str(p), "/MIR", "/NFL", "/NDL",
                        "/NJH", "/NJS", "/NP", "/R:0", "/W:0"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
        shutil.rmtree(p, ignore_errors=True)


def robocopy(src: Path, dst: Path, xd: list | None = None,
             xf: list | None = None, label: str = "") -> None:
    dst.mkdir(parents=True, exist_ok=True)
    cmd = ["robocopy", str(src), str(dst), "/E", "/MT:16", "/NFL", "/NDL",
           "/NJH", "/NJS", "/NP", "/R:1", "/W:1"]
    if xd:
        cmd.append("/XD")
        cmd += [str(p) for p in xd]
    if xf:
        cmd.append("/XF")
        cmd += list(xf)
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode >= 8:
        raise RuntimeError(f"robocopy {label} 失败 rc={r.returncode}\n{(r.stdout or '')[-1200:]}")


def masked(obj, keys=("secret_id", "secret_key", "tokenhub_api_key",
                      "openai_api_key")):
    """递归把指定键的字符串值换成占位符。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in keys:
                out[k] = "" if not v else PLACEHOLDER.get(k, PLACEHOLDER["api_key"])
            else:
                out[k] = masked(v, keys)
        return out
    if isinstance(obj, list):
        return [masked(v, keys) for v in obj]
    return obj


# ------------------------------------------------------------------ 各步
def stage_code() -> None:
    step(STEPS[0])
    for p in sorted(SRC.glob("*.py")):
        shutil.copy2(p, STAGE / p.name)
    for sub in ("recorder", "cloud", "tools", "models"):
        dst = STAGE / sub
        wipe(dst)
        robocopy(SRC / sub, dst, xd=["__pycache__", "prompts_backup"],
                 xf=["*.pyc", "*.log"], label=sub)
    (STAGE / "cloud" / "repair_20260911.py").unlink(missing_ok=True)

    bats = ["启动课堂控制台.bat", "start_all.bat", "start_recorder.bat",
            "start_auto_pipeline.bat", "start_monitor.bat",
            "start_voiceprint_gui.bat", "restart_pipeline_only.bat"]
    for b in bats:
        s = SRC / b
        if s.exists():
            shutil.copy2(s, STAGE / b)
    for b in DESKTOP.glob("*课堂系统.bat"):
        shutil.copy2(b, STAGE / b.name)

    (STAGE / "reports").mkdir(parents=True, exist_ok=True)
    tmpl = SRC / "reports" / "专注力数据录入表.xlsx"
    if tmpl.exists():
        shutil.copy2(tmpl, STAGE / "reports" / tmpl.name)
    say(f"    代码层完成")


def stage_ffmpeg() -> None:
    step(STEPS[1])
    if not FFMPEG_SRC.exists():
        raise SystemExit(f"找不到 {FFMPEG_SRC}")
    dst = STAGE / "ffmpeg" / "bin"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(FFMPEG_SRC, dst / "ffmpeg.exe")
    say(f"    ffmpeg.exe {os.path.getsize(dst / 'ffmpeg.exe') / MB:.1f} MB")


def stage_vendor() -> None:
    step(STEPS[2])
    vn = STAGE / "vendor" / "node"
    wipe(vn)
    vn.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PY_NODE, vn / "node.exe")
    # lark-cli：实测可独立运行（run.js 只用内置模块，真正的实现在原生 exe 里），
    # 因此不需要同目录那 85MB 的 @tencentcloud / @cloudbase / @wecom
    lk = vn / "lark-cli"
    robocopy(LARK_SRC, lk, xd=["__pycache__"], xf=["*.md", "*.pyc"], label="lark-cli")
    say(f"    node.exe {os.path.getsize(vn / 'node.exe') / MB:.1f} MB"
        f" | lark-cli {dir_size(lk):.1f} MB")

    vt = STAGE / "vendor" / "teaching_eval"
    wipe(vt)
    vt.mkdir(parents=True, exist_ok=True)
    if not PDF_GEN_SRC.exists():
        raise SystemExit(f"找不到 {PDF_GEN_SRC}")
    shutil.copy2(PDF_GEN_SRC, vt / "generate_pdf.py")
    say(f"    generate_pdf.py {os.path.getsize(vt / 'generate_pdf.py') / 1024:.1f} KB")


def stage_dirs() -> None:
    step(STEPS[3])
    for d in ("status", "logs", "reports", "cloud_work"):
        (STAGE / d).mkdir(parents=True, exist_ok=True)
    say("    status / logs / reports / cloud_work 就绪")


def stage_setup() -> None:
    step(STEPS[4])
    dst = STAGE / "_setup"
    wipe(dst)
    robocopy(FILES / "setup", dst, label="_setup")
    n = len(list(dst.iterdir())) if dst.exists() else 0
    say(f"    _setup：{n} 个文件")


def sanitize_configs() -> None:
    step(STEPS[5])
    p = STAGE / "cloud" / "cloud_config.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    n = sum(1 for sec, key in (("cos", "secret_id"), ("cos", "secret_key"),
                               ("llm", "tokenhub_api_key"))
            if cfg.get(sec, {}).get(key))
    p.write_text(json.dumps(masked(cfg), ensure_ascii=False, indent=2),
                 encoding="utf-8")

    p2 = STAGE / "recorder" / "config.json"
    cfg2 = json.loads(p2.read_text(encoding="utf-8"))
    n += sum(1 for key in ("secret_id", "secret_key")
             if cfg2.get("cos", {}).get(key))
    p2.write_text(json.dumps(masked(cfg2), ensure_ascii=False, indent=2),
                  encoding="utf-8")
    say(f"    已脱敏 {n} 处密钥")

    vp = [STAGE / "cloud" / "voiceprint_local_db.json",
          STAGE / "cloud" / "voiceprint_registry.json"]
    if INCLUDE_VOICEPRINT:
        say("    声纹库：已包含（INCLUDE_VOICEPRINT=True）")
    else:
        for f in vp:
            if f.exists():
                f.unlink()
        say("    声纹库：已排除（个人生物特征数据，装完在『声纹』页重新注册）")

    les = STAGE / "recorder" / "lessons.json"
    if les.exists():
        les.write_text('{\n  "lessons": []\n}\n', encoding="utf-8")
        say("    lessons.json 已重置为空课表")


def report() -> None:
    step(STEPS[6])
    venv_mb = dir_size(STAGE / "venv")
    total = dir_size(STAGE)
    say(f"    venv（运行时）    : {venv_mb:8.1f} MB")
    for sub in ("ffmpeg", "vendor", "models", "cloud", "recorder", "tools", "_setup"):
        p = STAGE / sub
        if p.exists():
            say(f"    {sub:<18}: {dir_size(p):8.1f} MB")
    say("    " + "-" * 36)
    say(f"    载荷合计          : {total:8.1f} MB")


if __name__ == "__main__":
    STAGE.mkdir(parents=True, exist_ok=True)
    if not (STAGE / "venv" / "python.exe").exists():
        raise SystemExit("✗ 请先运行 make_runtime.py（stage\\venv 不存在）")
    if not (FILES / "setup" / "setup.py").exists():
        raise SystemExit("✗ 请先运行 make_bats.py（files\\setup 不存在）")
    say(f"源      : {SRC}")
    say(f"载荷目录: {STAGE}")

    stage_code()
    stage_ffmpeg()
    stage_vendor()
    stage_dirs()
    stage_setup()
    sanitize_configs()
    report()
