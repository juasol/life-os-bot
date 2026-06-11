@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === life-os-bot を起動します ===
echo 停止するには Ctrl+C を押してください。
echo.
set PYTHONUTF8=1
".venv\Scripts\python.exe" main.py
echo.
echo === BOT が終了しました ===
pause
