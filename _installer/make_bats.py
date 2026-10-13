# -*- coding: utf-8 -*-
"""生成全部 .bat（CRLF + 纯 ASCII）与中文说明文件。

为什么用脚本生成：
  1. Windows 的 cmd 解析 .bat 按本地代码页（中文机是 GBK）。文件里出现 UTF-8
     的中文字节会被解成乱码，进而让 goto 的标签匹配失败 —— 表现为「脚本跑到
     一半就没了」。故所有 .bat **内容一律纯 ASCII**，中文只出现在文件名与
     由 python 打印的输出里。
  2. 行尾必须是 CRLF，LF-only 的批处理在部分 Windows 上行为异常。

产物：
  files/sfx/安装.bat              ← 放在 SFX 根，解压后双击它
  stage/_setup/*.bat              ← 安装后处理入口（ASCII 名，便于注册表引用）
  stage/首次配置.bat 等           ← 中文名便利入口，内容仍为 ASCII
"""
from __future__ import annotations

from pathlib import Path

INSTALLER = Path(r"C:\recorder\_installer")
FILES = INSTALLER / "files"
SFX = FILES / "sfx"
SETUP_T = FILES / "setup"          # 生成物模板
STAGE = INSTALLER / "build" / "stage"

CRLF = "\r\n"


def write_bat(path: Path, text: str) -> None:
    """写 .bat：强制 CRLF 与 ASCII。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = text.replace("\r\n", "\n").replace("\n", CRLF).lstrip("\n")
    path.write_bytes(body.encode("ascii"))
    print(f"  + {path.relative_to(INSTALLER)}  ({len(body)} bytes)")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"  + {path.relative_to(INSTALLER)}")


# ------------------------------------------------------------------ 安装入口
INSTALL_BAT = r"""
@echo off
rem ============================================================
rem  Classroom Auto-Eval System - installer entry
rem  ASCII-only on purpose: cmd parses .bat in the local codepage,
rem  and non-ASCII bytes break label/goto matching.
rem ============================================================
setlocal EnableExtensions
title Classroom Auto-Eval System - Setup
set "HERE=%~dp0"
set "ROOT=C:\recorder"

echo ============================================================
echo    Classroom Auto-Eval System  -  Setup
echo ============================================================
echo.

if not exist "%HERE%payload.7z" goto :nopayload
if not exist "%HERE%7za.exe" goto :no7za

echo Target directory : %ROOT%
if not exist "%ROOT%" goto :extract
echo.
echo [WARN] %ROOT% already exists. Its files will be overwritten.
set /p OVER=Continue? (Y/N):
if /I not "%OVER%"=="Y" goto :cancelled

:extract
echo.
echo [1/3] Extracting application and runtime ...
echo       This takes a few minutes, please wait.
"%HERE%7za.exe" x "%HERE%payload.7z" -o"%ROOT%" -y -bso0 -bsp1
if errorlevel 1 goto :failed

echo.
echo [2/3] Creating shortcuts ...
call "%ROOT%\_setup\install.bat" silent

echo.
echo [3/3] Environment self-check ...
"%ROOT%\venv\Scripts\python.exe" "%ROOT%\_setup\setup.py" check

echo.
echo ============================================================
echo    Setup finished.
echo.
echo    Next steps:
echo      1) Run "FirstConfig" to enter your cloud keys
echo         (Tencent Cloud SecretId / SecretKey / LLM API Key)
echo      2) Run _setup\lark_login.bat once for Feishu login
echo      3) Register the teacher voiceprint in the console
echo ============================================================
echo.
pause
exit /b 0

:nopayload
echo [ERROR] payload.7z not found next to this script.
pause
exit /b 1

:no7za
echo [ERROR] 7za.exe not found next to this script.
pause
exit /b 1

:cancelled
echo Cancelled by user.
pause
exit /b 1

:failed
echo.
echo [ERROR] Extraction failed. Nothing was installed.
pause
exit /b 1
"""

# ------------------------------------------------------------------ _setup 下的小入口
def setup_entry(script: str, use_pythonw: bool = False, title: str = "") -> str:
    exe = "pythonw.exe" if use_pythonw else "python.exe"
    return rf"""
@echo off
rem {title or script}
setlocal EnableExtensions
set "ROOT=C:\recorder"
if not exist "%ROOT%\venv\Scripts\{exe}" goto :nopy
"%ROOT%\venv\Scripts\{exe}" "%ROOT%\_setup\setup.py" {script}
set "RC=%errorlevel%"
if /I "%~1"=="silent" exit /b %RC%
echo.
pause
exit /b %RC%

:nopy
echo [ERROR] Python runtime missing: %ROOT%\venv\Scripts\{exe}
echo         The installation looks incomplete. Re-run Setup.
pause
exit /b 1
"""


LARK_LOGIN_BAT = r"""
@echo off
rem Feishu (Lark) device-flow login for the bundled lark-cli.
setlocal EnableExtensions
title Feishu Login
set "ROOT=C:\recorder"
set "NODE=%ROOT%\vendor\node\node.exe"
set "LARK=%ROOT%\vendor\node\lark-cli\scripts\run.js"

if not exist "%NODE%" goto :nonode
if not exist "%LARK%" goto :nolark

echo ============================================================
echo    Feishu / Lark Login (device flow)
echo ============================================================
echo.
echo  A browser window / URL will be shown. Complete the login
echo  there, then come back to this window.
echo.
"%NODE%" "%LARK%" auth login
set "RC=%errorlevel%"
echo.
if "%RC%"=="0" (
  echo [OK] Login finished.
) else (
  echo [WARN] Login returned code %RC%. You can re-run this script.
)
echo.
pause
exit /b %RC%

:nonode
echo [ERROR] node.exe not found: %NODE%
pause
exit /b 1

:nolark
echo [ERROR] lark-cli not found: %LARK%
pause
exit /b 1
"""


# ------------------------------------------------------------------ 中文名便利入口
def chinese_entry(target: str, label: str) -> str:
    return rf"""
@echo off
rem Convenience launcher for: {label}
setlocal
set "ROOT=C:\recorder"
call "{target}"
exit /b %errorlevel%
"""


CASUAL = {
    "首次配置.bat": ("%ROOT%\\_setup\\config.bat", "first-run configuration"),
    # 卸载走 Inno 原生卸载器（unins000.exe / 系统「应用」页），不再放根目录脚本
    "飞书登录.bat": ("%ROOT%\\_setup\\lark_login.bat", "feishu login"),
    "环境自检.bat": ("%ROOT%\\_setup\\check.bat", "environment self-check"),
}


README = """课堂录制自动教评系统 —— 使用说明
================================================

一、装完必须做的一步：填密钥
    双击本目录下的『首次配置.bat』，填入：
      · 腾讯云 SecretId / SecretKey   （访问管理 → API 密钥管理）
      · 大模型 API Key                 （TokenHub / DeepSeek）
    密钥只写在本机配置文件里，不会外发。
    ※ 安装包里带的是占位符，不填则上传与转写会失败。

二、飞书课表同步（装完必须做）
    双击『飞书登录.bat』，按提示在浏览器里完成一次授权；
    或打开控制台 →『飞书』页，点「登录飞书」（效果相同）。
    授权态存在 %USERPROFILE%\\.lark-cli，换机器需要重新登录一次。
    未登录也不会中断录制，只是课表匹配不到、课程会走「仅存档」。

三、选定授课教师（装完建议做）
    打开控制台 →『录课』页 →「授课教师」下拉栏，选择或输入授课教师姓名
    （需与飞书课表「老师」列一致），点「应用」。
    课程归属完全按此教师 + 飞书课表匹配，不再依赖声纹识别：
    · 课表触发的自动录制沿用课表里的真实教师，无需选择；
    · VAD / 手动触发的录制按所选教师上报，不选则无法匹配课表（仅存档）。

四、日常使用
    双击桌面『课堂录制控制台』。
    · 总览   —— 服务状态、自动守护、云端可达性
    · 录课   —— 授课教师选择、手动补录、分段与上传进度
    · 麦克风 —— 枚举设备、电平测试、一键校准
    · 提示词 —— 教评词句编辑（改完下一门课生效，无需重启）
    · 云端   —— 流水线状态与手动触发
    · 飞书   —— OAuth 登录 / 登出 / 授权状态

五、录制中不要做的事
    · 不要换麦克风（正在录的那一路已绑定旧设备）
    · 不要双击『强制重启课堂系统』（会把这节课截断）
    正常重启请用『一键重启课堂系统』，它在录制中会主动中止。

六、环境自检
    双击『环境自检.bat』可查看：Python 运行时、ffmpeg、PDF 生成器、
    Node、lark-cli、密钥与飞书登录态是否就绪。

七、卸载
    双击『卸载.bat』。程序目录 C:\\recorder 与本目录需手动删除。

八、目录说明
    venv\\              独立 Python 运行时（自带，无需系统装 Python）
    ffmpeg\\bin\\       录像/音频处理（自带）
    vendor\\node\\      Node 运行时 + lark-cli（飞书同步用）
    vendor\\teaching_eval\\  教评 PDF 生成器
    models\\            本地声纹模型（当前流程未用，保留备用）
    recorder\\  cloud\\ 系统本体
    status\\  logs\\  reports\\  cloud_work\\  运行期数据
"""


if __name__ == "__main__":
    print("生成 .bat 与说明文件 …")

    # SFX 根
    write_bat(SFX / "安装.bat", INSTALL_BAT)

    # _setup 模板（稍后由 make_payload 复制进 stage）
    write_bat(SETUP_T / "install.bat", setup_entry("install", title="install shortcuts"))
    write_bat(SETUP_T / "config.bat", setup_entry("config", title="first-run configuration"))
    write_bat(SETUP_T / "uninstall.bat", setup_entry("uninstall", title="uninstall"))
    write_bat(SETUP_T / "check.bat", setup_entry("check", title="environment self-check"))
    write_bat(SETUP_T / "lark_login.bat", LARK_LOGIN_BAT)
    write_text(SETUP_T / "使用说明.txt", README)

    # stage 根的中文名便利入口
    for name, (target, label) in CASUAL.items():
        write_bat(STAGE / name, chinese_entry(target, label))

    print("\n完成。")
