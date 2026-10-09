
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
