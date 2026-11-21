@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo 没有找到 Python 3，请先确认本机已安装 Python 3 并加入 PATH。
  pause
  exit /b 1
)
python -m server.app --open
if errorlevel 1 pause
