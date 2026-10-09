@echo off
rem Start the unified classroom console (no console window).
rem NOTE: the old monitor panel monitor_gui.py was replaced by console.py
rem       on 2026-09-15. This file is kept for backward compatibility and
rem       now launches console.py.
start "" "C:\recorder\venv\Scripts\pythonw.exe" "C:\recorder\console.py"
