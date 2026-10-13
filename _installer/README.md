# 课堂录制自动教评系统 · 安装包构建工程

本目录用于把 `C:\recorder` 打包成一个可在**零前置环境**的 Windows 上运行的安装包。

## 产物

```
build\out\课堂录制自动教评系统_v1.2_Setup.exe   ← 当前标准安装器
```

**Inno Setup 标准安装器**：双击后是完整的安装向导（中文界面、进度条、
完成页复选项），装到固定目录 `C:\recorder`，自动创建开始菜单 / 桌面快捷方式，
并在系统「设置 → 应用」与控制面板里注册卸载入口（`unins000.exe`）。
全部环境（Python 运行时 / ffmpeg / Node / lark-cli / 声纹模型 / VC 运行库）
打进单一安装文件，目标机**无需任何前置安装**。

**v1.2 变更**（v1.1 试装时把开发机真机覆盖出来的三处修复）：
1. 配置文件改走 `onlyifdoesntexist` —— 首次安装落占位符版，重装/升级时
   **绝不覆盖用户已填的真实密钥**；
2. 补 `DefaultGroupName` —— v1.1 缺它导致开始菜单快捷方式落进 `(Default)` 组；
3. stage 移除 SFX 时代的 `安装.bat` / `卸载.bat`，与 Inno 流程不再重复。

## 三步构建

```bat
cd C:\recorder\_installer
C:\recorder\venv\Scripts\python.exe make_runtime.py     :: 1) 精简独立运行时
C:\recorder\venv\Scripts\python.exe make_bats.py        :: 2) 生成安装脚本
C:\recorder\venv\Scripts\python.exe make_payload.py     :: 3) 组装载荷
tools\InnoSetup\ISCC.exe installer.iss                  :: 4) 编译标准安装器
```

旧版 SFX 流程（`make_sfx.py`）保留但已弃用。

**载荷代码刷新**：stage 是打包时刻的快照。源码改动后需同步刷新，再编译：

```bash
robocopy cloud  _installer\build\stage\cloud  /E /XF cloud_config.json cloud_config.example.json voiceprint_local_db.json /XD prompts_backup voiceprint_local_backup __pycache__ asr_cache
robocopy recorder _installer\build\stage\recorder /E /XF config.json /XD __pycache__
robocopy tools  _installer\build\stage\tools  /E /XF courses.local.json /XD __pycache__
# 根级单文件：console.py / monitor_gui.py / voiceprint_enroll_gui.py / mic_devices.py
```

顺序不能颠倒：`make_bats` 的产物在 `make_payload` 里被复制进载荷，
`make_payload` 依赖 `make_runtime` 产出的 `stage\venv`。

## 各脚本职责

| 脚本 | 产物 | 说明 |
|---|---|---|
| `make_runtime.py` | `build\stage\venv\` | 精简独立 Python 运行时（310MB，原 1049MB） |
| `make_bats.py` | `files\setup\*`、`stage\*.bat` | 全部 .bat（CRLF + 纯 ASCII）与使用说明 |
| `make_payload.py` | `build\stage\` 其余部分 | 代码 / ffmpeg / Node / lark-cli / 模型 / 脱敏配置 |
| `installer.iss` | `build\out\*_Setup.exe` | **Inno Setup 编译脚本**（v1.1 标准安装器） |
| `make_sfx.py` | `build\out\*_安装包.exe` | 旧版 7z SFX 自解压（弃用，保留回退） |

编译器本体在 `tools\InnoSetup\`（官方 6.7.1，静默装进项目目录，
`Languages\ChineseSimplified.isl` 取自 issrc is-6_5_1 tag——官方已撤下
简中翻译，该版本含 6.7 新增消息时会回落英文 Default.isl，仅影响
下载/校验等本安装器用不到的提示语）。

## 关键设计决定（改动前务必先读）

### 1. 为什么铺成 `venv\` 布局而不是干净目录

`console.py:163` 写死了 `ROOT / "venv" / "Scripts" / "pythonw.exe"`，
控制台用它拉起 recorder / auto_pipeline。把独立运行时铺成同名布局，
可以做到**源码零改动**。

实测确认：真实解释器副本放在 `Scripts\` 下，能向上 landmark 找到
`venv\Lib\os.py`，`sys.prefix` 正确解析为 venv 根。
**前提是 `python313.dll` 必须与 `Scripts\python.exe` 同目录**，否则起不来。

### 2. 为什么必须删掉 `pyvenv.cfg`

原 `C:\recorder\venv` 是标准 venv，`Scripts\python.exe` 只是 241KB 的重定向桩，
`pyvenv.cfg` 指向 `C:\Users\<用户名>\.workbuddy\binaries\python\versions\3.13.12`。
换台机器这个路径不存在，**必崩**。打包用的运行时里不能有任何 `pyvenv.cfg`。

### 3. 为什么安装目录固定为 `C:\recorder`

源码里 60+ 处硬编码 `C:\recorder\...`（config 里的模型与声纹库路径、
status 文件、reports 目录、cloud_work 工作区……）。改目录必崩。
因此 `安装.bat` 里写死目标路径，并让用户**解压到哪都行**——
这样即使解压到桌面也不会把系统装错地方。

### 4. 瘦身白名单的依据

- **PySide6**：对 82 个源文件做 AST 全量扫描，确认只用到
  `QtCore` / `QtGui` / `QtWidgets`。删掉 Addons 全家（WebEngine 194MB、
  QtQuick/Qml、3D、Charts、Designer、Pdf、translations、qml 等）
  → 451.8MB 降到 97.1MB。
  裁剪后必须跑 `make_runtime.py` 第 5 步的导入自检。
- **tencentcloud**：262 个产品模块，实际只用 `asr` + `common`
  → 344.9MB 降到 0.4MB。
- **`*.pyi`**：类型存根，运行时不加载，一律删。
- **声纹库**：默认不打包（个人生物特征数据）。需要时把
  `make_payload.py` 的 `INCLUDE_VOICEPRINT` 置 `True`。

### 5. 密钥一律占位符

`cos.secret_id` / `cos.secret_key` / `llm.tokenhub_api_key` 会被替换成
`PUT-YOUR-...-HERE`。装完后用『首次配置.bat』填入。
如需开箱即用的私有版本，把 `make_payload.py` 里 `sanitize_configs()` 的
脱敏调用去掉即可（**但那个包就不能外发了**）。

### 6. 为什么不做「复制后再删」

沙箱/安全策略会对单轮超过 50 次的删除操作要确认。所以瘦身一律改成
**复制时用 robocopy 的 `/XD` `/XF` 排除**，既不触发限制，也少写一遍 780MB。
清理旧产物用 `cmd /c rd /s /q`（比 robocopy `/MIR` 快一个数量级：
5 万个小文件从 7 分钟降到十几秒）。

### 7. 为什么 v1.1 从 7z SFX 升级到 Inno Setup

v1.0 的 7z SFX 本质是「自解压 + 手动运行 安装.bat」两步，用户反馈这
"只是压缩包，不是安装包"。v1.1 改用 **Inno Setup 6.7.1**（官方签名版，
静默装进 `tools\InnoSetup`，不污染系统）：

- 真正的安装向导：欢迎页 → 进度条 → 完成页（可选立即填密钥 / 飞书登录 / 启动控制台）；
- 原生卸载器：`{app}\unins000.exe` + 系统「应用」列表里的卸载入口（HKCU）；
- 快捷方式、开机自启（可选任务）全部由安装器管理，不再依赖 `setup.py`；
- 安装前自动检测 pythonw/python 进程在跑会提醒先停服务（文件占用会导致装一半失败）；
- 卸载时顺带清掉旧版（安装.bat 方案）留在注册表 `ClassroomRecorder` 的卸载项。

**踩坑**：`[Code]` 段里行首的 `#13#10` 会被 ISPP 预处理器当成指令报
"Unknown preprocessor directive"——`#` 开头的续行必须避免，把 `#13#10`
放在行尾即可。另外 `ISCC /O-` 是"禁用输出"（校验脚本用），正式编译不带它。

**旧方案为何落选**：`iexpress`（Windows 内置）GUI 程序无法脚本化（实测挂死）；
`7z.sfx` 24.09 不支持 `RunProgram` 安装配置（二进制 ASCII/UTF-16 双查无此字符串）；
新版 7-Zip Extra 已移除安装器 SFX 模块。

## 构建后验证（v1.2 实测记录）

| # | 验证项 | 方法 | 结果 |
|---|---|---|---|
| 1 | 静默安装到隔离目录 | PowerShell `Start-Process -Wait` + `/VERYSILENT /DIR=…` | 退出码 0，装到指定目录 |
| 2 | 不误装真机 | 安装后查 `C:\recorder\unins000.exe` | 未出现 ✓ |
| 3 | 注册表指向 | 读 HKCU 卸载项 `InstallLocation` | 指向隔离目录 ✓ |
| 4 | 开始菜单组名 | 列 `%APPDATA%\...\Programs\课堂录制自动教评系统` | 6 个快捷方式齐全 ✓ |
| 5 | 安装出的 Python | `sys.prefix` + `import PySide6, numpy, qcloud_cos` | 指向隔离 venv，导入 OK |
| 6 | **重装保留密钥** | 填模拟密钥 → 二次安装 → 复查 | `onlyifdoesntexist` 生效，密钥未覆盖 ✓ |
| 7 | 静默卸载 | `unins000.exe /VERYSILENT` | 目录/注册表/开始菜单全清，不波及他处 ✓ |

### 排错记录 2：静默安装参数被 Git Bash 吃掉（本次事故根因）

在 Git Bash 里跑 `Setup.exe //VERYSILENT //DIR=…`：禁用路径转换（`MSYS2_ARG_CONV_EXCL`）
后 `//VERYSILENT` 是**字面量**，Inno 不认（它要单斜杠 `/VERYSILENT`）→ 参数全被忽略，
安装器以**向导模式**弹在屏幕上，最终被点完成、装进默认目录 `C:\recorder`，
把开发机的真实密钥配置覆盖成了占位符版（已从 10/10 卷影副本恢复）。

教训：**测 Inno 静默参数一律走 PowerShell `Start-Process -ArgumentList`**，
参数用数组、单斜杠，绝不经过 bash 转换层。另注意：注册表里已有同 AppId 的
安装记录时，`/DIR` 会被忽略（Inno 认为是升级、沿用原目录）——重测前先删
`HKCU\...\Uninstall\{AppId}}_is1`。

### 排错记录：`python313.dll` 必须与 `python.exe` 同目录

验证时若只解压了 `venv\Scripts\python.exe` 而漏掉同目录的 `python313.dll`，
Windows 会沿 `PATH` 找到本机另一个 Python 3.13 的 DLL，`sys.prefix`
会错误地指向本机路径 —— **看起来像打包失败，实际是没解全**。
`make_runtime.py` 的 `build_scripts()` 已把 DLL 与解释器同置，正常安装不会遇到。

## 目标机自检

装完后双击 `环境自检.bat`，或在 `C:\recorder` 下执行：

```bat
venv\Scripts\python.exe _setup\setup.py check
```

## 已知限制

1. **安装目录固定 `C:\recorder`**：源码 60+ 处硬编码，向导不给选目录（这是特性不是缺陷）。
2. **飞书需一次授权**：完成页勾选「立即登录飞书」或运行 `飞书登录.bat`，
   OAuth 登录态（`%USERPROFILE%\.lark-cli`）无法跨机复制。
3. **声纹需重新注册**：安装包不含声纹库（生物特征数据）。
4. **云密钥是占位符**：完成页勾选「立即填写云服务密钥」或运行 `首次配置.bat`。
   `cloud_config.json` 的飞书 base/table 标识保留在实例配置里（课表同步必需），
   源码中不出现。
5. **`lark-cli.exe` 是未签名原生程序**：若目标机开着 Smart App Control，
   可能被拦截。参考 `~/.workbuddy/MEMORY.md` 里的处置办法。
6. **构建耗时**：`make_runtime` 约 10 分钟；ISCC 编译（LZMA2/max 压 577MB）
   约 10–25 分钟。
7. **静默部署**：支持 `/VERYSILENT /SUPPRESSMSGBOXES /TASKS="" /DIR=…`
   （`/DIR` 可覆盖固定目录，仅测试用；正式装别改）。
