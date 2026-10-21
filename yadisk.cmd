@echo off
setlocal
cd /d "%~dp0"
if exist YaDiskDownloader-CLI.exe (
  YaDiskDownloader-CLI.exe %*
  exit /b
)
if not exist .venv\Scripts\python.exe (
  echo Сначала запустите start.cmd, чтобы установить Python-зависимости.
  exit /b 2
)
.venv\Scripts\python.exe -m disk_downloader %*
