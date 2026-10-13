
@echo off
rem Convenience launcher for: feishu login
setlocal
set "ROOT=C:\recorder"
call "%ROOT%\_setup\lark_login.bat"
exit /b %errorlevel%
