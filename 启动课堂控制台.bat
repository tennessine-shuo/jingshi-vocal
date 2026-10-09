@echo off
rem Classroom Recording System - Unified Console
rem One window: recorder service / voiceprint / microphone / cloud pipeline
rem Console manages the background services itself (start / restart / auto-guard)
start "" "C:\recorder\venv\Scripts\pythonw.exe" "C:\recorder\console.py"
exit
