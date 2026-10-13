@echo off
title Classroom Recording System - Safe Restart

echo ============================================================
echo    Classroom Recording System  --  Safe Restart
echo ============================================================
echo.
echo   * Stops the old services, then starts the LATEST code.
echo   * If a lesson is being recorded RIGHT NOW, this script will
echo     ABORT, so that lesson's audio is never cut short.
echo     Just run it again after the lesson ends.
echo   * Need to restart anyway? Use Force Restart.bat instead.
echo.
echo   Project : C:\recorder
echo.

set "PY=C:\recorder\venv\Scripts\python.exe"
set "SCRIPT=%USERPROFILE%\Desktop\restart_scheduled.py"

if not exist "%PY%"     ( echo   [ERROR] Python not found: "%PY%" & goto :end )
if not exist "%SCRIPT%" ( echo   [ERROR] Script not found: "%SCRIPT%" & goto :end )

"%PY%" "%SCRIPT%"

:end
echo.
echo ============================================================
echo    Finished. Press any key to close this window.
echo ============================================================
pause >nul
