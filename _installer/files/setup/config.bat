
@echo off
rem first-run configuration
setlocal EnableExtensions
set "ROOT=C:\recorder"
if not exist "%ROOT%\venv\Scripts\python.exe" goto :nopy
"%ROOT%\venv\Scripts\python.exe" "%ROOT%\_setup\setup.py" config
set "RC=%errorlevel%"
if /I "%~1"=="silent" exit /b %RC%
echo.
pause
exit /b %RC%

:nopy
echo [ERROR] Python runtime missing: %ROOT%\venv\Scripts\python.exe
echo         The installation looks incomplete. Re-run Setup.
pause
exit /b 1
