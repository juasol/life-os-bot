@echo off
cd /d "%~dp0"
echo === Starting life-os-bot ===
echo Press Ctrl+C to stop.
echo.
set PYTHONUTF8=1

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py
) else (
    python main.py
)

echo.
echo === Bot stopped ===
pause
