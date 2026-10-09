
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
