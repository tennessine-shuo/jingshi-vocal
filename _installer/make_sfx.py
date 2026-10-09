# -*- coding: utf-8 -*-
"""把 build/stage 打成单文件安装包（7z SFX）。

为什么用 7z SFX：
  · 本机已有签名版 7-Zip，7z.sfx 是官方自解压模块，不引入第三方安装器；
  · Inno Setup / NSIS 需另行安装（且有 Smart App Control 拦截未签名程序的历史）；
  · iexpress 是 GUI 程序，脚本化调用会挂住（已实测，退出码 1）。

结构（为什么是「两层」）：
  最终 exe = 7z.sfx + outer.7z
  outer.7z 里只有三个文件：
      安装.bat     ← 解压后双击它
      7za.exe      ← 由安装包自带，目标机不需要装 7-Zip
      payload.7z   ← 真正的系统本体（约 800MB）
  这样做的好处：**解压位置随便选**。安装.bat 内部固定装到 C:\\recorder
  （系统代码有 60+ 处写死该路径，换目录必崩），因此用户即使解压到桌面
  也不会把系统装错地方 —— 这是相比「直接解压到 C:\\recorder」最关键的安全点。

用法：<出口python> make_sfx.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

INSTALLER = Path(r"C:\recorder\_installer")
BUILD = INSTALLER / "build"
STAGE = BUILD / "stage"
FILES = INSTALLER / "files"
SFX_DIR = FILES / "sfx"
OUT = BUILD / "out"

SEVEN_ZIP = Path(r"C:\Program Files\7-Zip\7z.exe")
SFX_STUB = Path(r"C:\Program Files\7-Zip\7z.sfx")

PKG_NAME = "课堂录制自动教评系统_v1.0_安装包.exe"
PKG_DIRNAME = "课堂录制自动教评系统_安装包"   # 外层套的文件夹名
PAYLOAD = "payload.7z"          # 内层：系统本体
OUTER = "outer.7z"              # 外层：待拼进 SFX

MB = 1048576


def say(msg: str) -> None:
    print(msg, flush=True)


def run(cmd: list, label: str = "", cwd: Path | None = None) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd) if cwd else None,
                       encoding="utf-8", errors="replace")
    if r.returncode >= 2:
        raise SystemExit(f"✗ {label} 失败 rc={r.returncode}\n{(r.stdout or '')[-2000:]}\n"
                         f"{(r.stderr or '')[-1000:]}")


def sz(p: Path) -> float:
    return p.stat().st_size / MB if p.exists() else 0.0


def main() -> None:
    for p, what in ((SEVEN_ZIP, "7-Zip 主程序"), (SFX_STUB, "7z.sfx 模块")):
        if not p.exists():
            raise SystemExit(f"✗ 找不到{what}：{p}")
    if not (STAGE / "venv" / "python.exe").exists():
        raise SystemExit("✗ stage\\venv 不存在，请先运行 make_runtime.py")
    if not (SFX_DIR / "安装.bat").exists():
        raise SystemExit("✗ files\\sfx\\安装.bat 不存在，请先运行 make_bats.py")

    OUT.mkdir(parents=True, exist_ok=True)
    payload = SFX_DIR / PAYLOAD

    # ---------------------------------------------------------- 1) 内层：载荷
    say("[1/3] 压缩系统本体 → payload.7z（LZMA2，这一步最慢）…")
    if payload.exists() and "--rebuild" not in sys.argv:
        say(f"    复用已有 payload.7z（{sz(payload):.1f} MB）")
        say("    提示：stage 有改动时加 --rebuild 强制重压")
    else:
        if payload.exists():
            payload.unlink()
        t0 = time.time()
        # cwd=STAGE + "*"：保证归档根就是 stage 的内容，不会多出一层 stage\ 目录
        # （否则解压会变成 C:\recorder\stage\...，系统直接找不到文件）
        # -mx=7：压缩率与耗时的平衡点；-mmt=on 多线程
        run([str(SEVEN_ZIP), "a", "-t7z", "-m0=lzma2", "-mx=7", "-mmt=on",
             "-y", str(payload), "*"], "内层压缩", cwd=STAGE)
        say(f"    完成 {sz(payload):.1f} MB（用时 {time.time() - t0:.0f}s）")

    # ---------------------------------------------------------- 2) 外层：三件套
    say("\n[2/3] 打包外层（套一层文件夹，避免解压后文件散落）…")
    outer = OUT / OUTER
    if outer.exists():
        outer.unlink()
    # 归档里套一层同名目录：解压后得到的是一个干净文件夹，而不是把
    # 安装.bat / 7za.exe / 144MB 的 payload.7z 直接摊在用户选的目录里。
    pkg_root = SFX_DIR / "_pkg"
    if pkg_root.exists():
        shutil.rmtree(pkg_root, ignore_errors=True)
    box = pkg_root / PKG_DIRNAME
    box.mkdir(parents=True, exist_ok=True)
    for n in ("安装.bat", "7za.exe", PAYLOAD):
        src = SFX_DIR / n
        try:
            os.link(src, box / n)          # 同盘硬链接：零拷贝、零额外空间
        except OSError:
            shutil.copy2(src, box / n)     # 跨盘时退回复制
    run([str(SEVEN_ZIP), "a", "-t7z", "-mx=0", "-y", str(outer), PKG_DIRNAME],
        "外层打包", cwd=pkg_root)
    shutil.rmtree(pkg_root, ignore_errors=True)
    say(f"    outer.7z {sz(outer):.1f} MB（内含 {PKG_DIRNAME}\\）")

    # ---------------------------------------------------------- 3) 拼 SFX
    say("\n[3/3] 拼接自解压头 → 安装包 …")
    out_exe = OUT / PKG_NAME
    if out_exe.exists():
        out_exe.unlink()
    with open(out_exe, "wb") as w:
        for part in (SFX_STUB, outer):
            with open(part, "rb") as r:
                shutil.copyfileobj(r, w, length=8 * 1024 * 1024)
    say(f"    ✓ {out_exe.name}  {sz(out_exe):.1f} MB")

    # ---------------------------------------------------------- 清点
    say("\n" + "=" * 60)
    say(f"安装包：{out_exe}")
    say(f"大小  ：{sz(out_exe):.1f} MB")
    say("=" * 60)
    say(f"\n使用：双击安装包 → 解压出「{PKG_DIRNAME}」文件夹 → 进去运行『安装.bat』")
    say("      安装.bat 会把系统装到 C:\\recorder（固定，不可改）")


if __name__ == "__main__":
    main()
