@echo off
title Classroom Recording System - FORCE Restart

echo ============================================================
echo    Classroom Recording System  --  FORCE Restart
echo ============================================================
echo.
echo   WARNING
echo   If a lesson is being recorded, this will STOP recording it
echo   immediately. The rest of that lesson will NOT be captured.
echo.
echo   Use Safe Restart.bat in normal cases.
echo   Press Ctrl+C now if you do not want this.
echo.
echo   Project : C:\recorder
echo.

set "PY=C:\recorder\venv\Scripts\python.exe"
set "SCRIPT=%USERPROFILE%\Desktop\restart_scheduled.py"

if not exist "%PY%"     ( echo   [ERROR] Python not found: "%PY%" & goto :end )
if not exist "%SCRIPT%" ( echo   [ERROR] Script not found: "%SCRIPT%" & goto :end )

"%PY%" "%SCRIPT%" --force

:end
echo.
echo ============================================================
echo    Finished. Press any key to close this window.
echo ============================================================
pause >nul
