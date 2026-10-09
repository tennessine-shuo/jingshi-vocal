@echo off
rem Classroom Recorder launcher: cmd 一闪即关，pythonw 独立后台运行
rem Logs: C:\recorder\logs\recorder.log
cd /d C:\recorder\recorder
start "" "C:\recorder\venv\Scripts\pythonw.exe" recorder.py
exit
