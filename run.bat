@echo off
title DingDingDong — Discord Music Bot
cd /d "%~dp0"
set PYTHONUTF8=1
echo Starting DingDingDong...
".venv\Scripts\python.exe" -m bot.main
echo.
echo Bot exited (code %errorlevel%). Press any key to close.
pause >nul
