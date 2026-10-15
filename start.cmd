@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "launcher_exit=%errorlevel%"
if not "%launcher_exit%"=="0" pause
exit /b %launcher_exit%
