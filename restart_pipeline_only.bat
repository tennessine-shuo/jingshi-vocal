@echo off
rem Restart ONLY pipeline + console with the latest code.
rem The recorder (pythonw running recorder.py) is NOT touched,
rem so the ongoing lesson recording keeps running.
rem Safe to double-click anytime; singleton locks prevent duplicates.

echo Stopping old pipeline and console (recorder keeps recording)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and $_.CommandLine -match 'auto_pipeline|console\.py|monitor_gui' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 2 /nobreak >nul

echo Starting pipeline + console (new code, no flashing windows)...
cd /d C:\recorder\cloud
start "" "C:\recorder\venv\Scripts\pythonw.exe" auto_pipeline.py

cd /d C:\recorder
start "" "C:\recorder\venv\Scripts\pythonw.exe" console.py

echo Done. Recorder was not affected. You can close this window.
timeout /t 4 >nul
exit
