
@echo off
rem Convenience launcher for: environment self-check
setlocal
set "ROOT=C:\recorder"
call "%ROOT%\_setup\check.bat"
exit /b %errorlevel%
