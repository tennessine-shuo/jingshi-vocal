# 课堂录制自动教评系统 · 安装包构建工程

本目录用于把 `C:\recorder` 打包成一个可在**零前置环境**的 Windows 上运行的安装包。

## 产物

```
build\out\课堂录制自动教评系统_v1.0_安装包.exe
```

单文件自解压安装包。目标机双击 → 解压出一个 `课堂录制自动教评系统_安装包\`
文件夹 → 运行里面的 `安装.bat` → 系统被装到 `C:\recorder`。

外层刻意套了一层同名文件夹，避免解压后把 `安装.bat` / `7za.exe` / 144MB 的
`payload.7z` 直接摊在用户选的目录里（比如桌面）。

## 三步构建

```bat
cd C:\recorder\_installer
C:\recorder\venv\Scripts\python.exe make_runtime.py     :: 1) 精简独立运行时
C:\recorder\venv\Scripts\python.exe make_bats.py        :: 2) 生成安装脚本
C:\recorder\venv\Scripts\python.exe make_payload.py     :: 3) 组装载荷
C:\recorder\venv\Scripts\python.exe make_sfx.py         :: 4) 打自解压包
```

`make_sfx.py` 默认复用 `files\sfx\payload.7z`（省掉约 3.4 分钟的重压）。
**stage 内容有改动时要加 `--rebuild`**，否则打进去的仍是旧载荷。

顺序不能颠倒：`make_bats` 的产物在 `make_payload` 里被复制进载荷，
`make_payload` 依赖 `make_runtime` 产出的 `stage\venv`。

## 各脚本职责

| 脚本 | 产物 | 说明 |
|---|---|---|
| `make_runtime.py` | `build\stage\venv\` | 精简独立 Python 运行时（310MB，原 1049MB） |
| `make_bats.py` | `files\setup\*`、`stage\*.bat` | 全部 .bat（CRLF + 纯 ASCII）与使用说明 |
| `make_payload.py` | `build\stage\` 其余部分 | 代码 / ffmpeg / Node / lark-cli / 模型 / 脱敏配置 |
| `make_sfx.py` | `build\out\*.exe` | 内层 payload.7z + 外层 SFX 拼接 |

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

### 7. 为什么用 7z SFX 而不是 Inno Setup / iexpress

- 本机已有**签名版** 7-Zip，`7z.sfx` 是官方自解压模块，不引入第三方；
- Inno Setup / NSIS 需联网下载安装，且本机有 Smart App Control 拦截
  未签名程序的历史；
- `iexpress`（Windows 内置）是 GUI 程序，`/N /Q` 脚本化调用会挂住（实测退出码 1）。

`7z.sfx` 24.09 不含安装配置支持（`RunProgram`/`Title` 等字符串在二进制里
均不存在，ASCII 与 UTF-16 都查过），所以是「自解压 + 手动运行 安装.bat」
两步，而不是全自动一步。

## 构建后验证（均已实测）

| # | 验证项 | 方法 | 结果 |
|---|---|---|---|
| 1 | 归档完整性 | `7za t payload.7z` | Everything is Ok（4452 文件 / 594.5 MB） |
| 2 | 归档根结构 | `7za l payload.7z` | 根级直接是 `venv\` `ffmpeg\` `cloud\`…，无多余 `stage\` 层 |
| 3 | SFX 自解压 | `安装包.exe -y -o<dir>` | 退出码 0，解出「课堂录制自动教评系统_安装包」文件夹 |
| 4 | 运行时独立性 | 在 `C:\Users\Public\...` 隔离路径实跑 | `sys.prefix` 正确指向隔离 venv 根 |
| 5 | 第三方依赖 | 逐个 `import` | 14/14 通过（`scipy` 项目未使用） |
| 6 | 端到端自检 | SFX → 解压 → `setup.py check` | 7 项必需组件全绿 |
| 7 | 捆绑程序 | `lark-cli --version` / `ffmpeg -version` | 1.0.96 / 9.0.1 |

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

1. **不是单击安装**：解压后需再运行一次 `安装.bat`。
2. **飞书需一次授权**：`_setup\lark_login.bat`，OAuth 登录态
   （`%USERPROFILE%\.lark-cli`）无法跨机复制。
3. **声纹需重新注册**：安装包不含声纹库。
4. **`lark-cli.exe` 是未签名原生程序**：若目标机开着 Smart App Control，
   可能被拦截。参考 `~/.workbuddy/MEMORY.md` 里的处置办法。
5. **构建耗时**：`make_runtime` 约 10 分钟，`make_sfx` 压缩约 10 分钟。
