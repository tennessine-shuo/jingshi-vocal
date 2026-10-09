@echo off
rem Classroom System One-Click Launcher
rem Starts: recorder daemon + auto pipeline daemon + unified console
rem All run as independent background processes (no console windows)

cd /d C:\recorder\recorder
start "" "C:\recorder\venv\Scripts\pythonw.exe" recorder.py

cd /d C:\recorder\cloud
start "" "C:\recorder\venv\Scripts\pythonw.exe" auto_pipeline.py

cd /d C:\recorder
start "" "C:\recorder\venv\Scripts\pythonw.exe" console.py

exit
