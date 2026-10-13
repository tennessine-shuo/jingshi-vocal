; =====================================================================
; 课堂录制自动教评系统 - 标准安装器脚本（Inno Setup 6）
;
; 构建:  tools\InnoSetup\ISCC.exe installer.iss
; 产物:  build\out\课堂录制自动教评系统_v1.1_Setup.exe
;
; 与旧 7z SFX 方案的区别：
;   - 真正的安装向导（中文界面、进度条、完成页）
;   - 原生卸载器 unins000.exe，出现在系统「设置-应用」与控制面板
;   - 开始菜单 / 桌面快捷方式、可选开机自启，全部由安装器管理
;   - 单文件自带全部环境（Python 运行时 / ffmpeg / Node / lark-cli /
;     声纹模型 / VC 运行库），目标机无需任何前置安装
;
; 内容源: build\stage\ （由 make_runtime.py + make_payload.py 构建，
;         代码已用脱敏后的源码刷新，详见 README.md）
; =====================================================================

#define MyAppName "课堂录制自动教评系统"
#define MyAppVersion "1.3.0"

[Setup]
; 固定 AppId：换版本升级时能识别同一安装
AppId={{3057CEC1-A280-42D7-A847-6499FBBD4BA5}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersion}
; 不写公司名，避免不实署名
AppPublisher=
DefaultDirName=C:\recorder
; 目录固定（源码 60+ 处按 C:\recorder 硬编码），不给用户改目录的机会
DisableDirPage=yes
DisableProgramGroupPage=yes
DefaultGroupName={#MyAppName}
DisableWelcomePage=no
; 装到 C:\recorder 只需在 C 盘根建目录（标准用户即可），无需管理员
PrivilegesRequired=lowest
; 按用户级写入注册表（HKCU），卸载入口出现在当前用户的「应用」列表
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\venv\Scripts\pythonw.exe
SetupIconFile=build\app.ico
WizardStyle=modern
ShowLanguageDialog=no
CloseApplications=no
RestartApplications=no
; LZMA2 极限压缩：577MB 载荷 -> ~140MB
Compression=lzma2/max
SolidCompression=yes
OutputDir=build\out
OutputBaseFilename=课堂录制自动教评系统_v1.3_Setup
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式(&D)"; GroupDescription: "附加任务:"
Name: "autostart"; Description: "开机自动启动控制台（后台待命，不自动录音）(&A)"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
; 系统本体：stage 即目标机 C:\recorder 的完整形态
; ⚠ 配置文件不随主载荷安装：已填好的真实密钥不能被重装覆盖回占位符。
;   config 两件单独走 onlyifdoesntexist（首次安装落占位符版，重装时保留用户已填值）；
;   lessons.json / voiceprint_registry.json 属运行数据，永不安装。
Source: "C:\recorder\_installer\build\stage\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "cloud\cloud_config.json,recorder\config.json,recorder\lessons.json,cloud\voiceprint_registry.json"
Source: "C:\recorder\_installer\build\stage\cloud\cloud_config.json"; DestDir: "{app}\cloud"; Flags: ignoreversion onlyifdoesntexist
Source: "C:\recorder\_installer\build\stage\recorder\config.json"; DestDir: "{app}\recorder"; Flags: ignoreversion onlyifdoesntexist

[Registry]
; 可选：开机自启控制台（pythonw 无窗口）
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "ClassroomRecorderConsole"; ValueData: """{app}\venv\Scripts\pythonw.exe"" ""{app}\console.py"""; Flags: uninsdeletevalue; Tasks: autostart

[Icons]
Name: "{group}\课堂录制控制台"; Filename: "{app}\venv\Scripts\pythonw.exe"; Parameters: """{app}\console.py"""; WorkingDir: "{app}"; Comment: "打开课堂录制控制台（录课 / 声纹 / 麦克风 / 提示词 / 云端）"
Name: "{group}\一键重启课堂系统"; Filename: "{app}\一键重启课堂系统.bat"; WorkingDir: "{app}"; Comment: "录制中会中止以防截断课程"
Name: "{group}\强制重启课堂系统"; Filename: "{app}\强制重启课堂系统.bat"; WorkingDir: "{app}"; Comment: "忽略录制状态强制重启（可能截断正在录的课）"
Name: "{group}\环境自检"; Filename: "{app}\环境自检.bat"; WorkingDir: "{app}"; Comment: "检查运行环境与云服务配置"
Name: "{group}\{#MyAppName} 使用说明"; Filename: "{app}\_setup\使用说明.txt"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"; Comment: "卸载本系统（录音数据目录会询问是否保留）"
Name: "{autodesktop}\课堂录制控制台"; Filename: "{app}\venv\Scripts\pythonw.exe"; Parameters: """{app}\console.py"""; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; 完成页复选项（静默安装时自动跳过）
Filename: "{app}\首次配置.bat"; Description: "立即填写云服务密钥（腾讯云 / LLM，首次必填）"; Flags: postinstall nowait skipifsilent
Filename: "{app}\飞书登录.bat"; Description: "立即登录飞书（课表同步需要，每台电脑一次）"; Flags: postinstall nowait unchecked skipifsilent
Filename: "{app}\venv\Scripts\pythonw.exe"; Parameters: """{app}\console.py"""; WorkingDir: "{app}"; Description: "立即启动控制台"; Flags: postinstall nowait unchecked skipifsilent

[UninstallRun]
; 留空：录音数据（C:\recordings）与运行数据（reports/logs/cloud_work）默认保留

[Code]
// ---------- 进程检测 ----------
function IsProcessRunning(const ImageName: String): Boolean;
var
  TmpFile: String;
  Lines: TArrayOfString;
  I, rc: Integer;
  ImgUpper: String;
begin
  Result := False;
  TmpFile := ExpandConstant('{tmp}\tasklist.csv');
  if not Exec(ExpandConstant('{cmd}'),
       '/C tasklist /FO CSV /NH > "' + TmpFile + '"',
       '', SW_HIDE, ewWaitUntilTerminated, rc) then
    Exit;
  if not LoadStringsFromFile(TmpFile, Lines) then
    Exit;
  ImgUpper := Uppercase(ImageName);
  for I := 0 to GetArrayLength(Lines) - 1 do
    if Pos('"' + ImgUpper + '"', Uppercase(Lines[I])) > 0 then
    begin
      Result := True;
      Exit;
    end;
end;

// ---------- 安装前：检测系统是否在运行 ----------
// 用 SuppressibleMsgBox：/VERYSILENT 静默安装（升级脚本场景）自动跳过，
// 交互安装照常提醒。此前用普通 MsgBox，静默安装遇到任一 python 进程
// （含与本系统无关的）会卡在无人点击的对话框上。
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if IsProcessRunning('pythonw.exe') or IsProcessRunning('python.exe') then
  begin
    if SuppressibleMsgBox(
        '检测到可能有正在运行的程序（pythonw / python 进程）。' #13#10 +
        '若本系统正在录制或控制台正在运行，继续安装会因文件被占用而失败。' #13#10 #13#10 +
        '建议：先在控制台停止所有服务，再重新运行安装。' #13#10 #13#10 +
        '确认已停止、仍要继续安装吗？',
        mbConfirmation, MB_YESNO, IDYES) = IDNO then
      Result := '已取消安装：请先停止正在运行的服务。';
  end;
end;

// ---------- 卸载前：同样提醒 ----------
function UninstallPossible: Boolean;
begin
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    if IsProcessRunning('pythonw.exe') or IsProcessRunning('ffmpeg.exe') then
      SuppressibleMsgBox(
        '检测到可能仍在运行的服务（pythonw / ffmpeg 进程）。' #13#10 +
        '卸载可能删不干净，建议先停止服务再重试。' #13#10 +
        '录制数据目录 C:\recordings 与报告目录不会被删除。',
        mbInformation, MB_OK, IDOK);
  end;
  // 清理旧版（安装.bat 方案）留下的卸载注册项，避免出现两个卸载入口
  if CurUninstallStep = usPostUninstall then
    RegDeleteKeyIncludingSubkeys(HKCU,
      'Software\Microsoft\Windows\CurrentVersion\Uninstall\ClassroomRecorder');
end;
