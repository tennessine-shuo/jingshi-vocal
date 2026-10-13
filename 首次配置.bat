
@echo off
rem Convenience launcher for: first-run configuration
setlocal
set "ROOT=C:\recorder"
call "%ROOT%\_setup\config.bat"
exit /b %errorlevel%
