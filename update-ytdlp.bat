@echo off
title Update yt-dlp
cd /d "%~dp0"
echo Updating yt-dlp (fixes most "stream failed" issues)...
".venv\Scripts\python.exe" -m pip install -U yt-dlp
echo.
echo Done. Restart the bot for the update to take effect.
pause >nul
