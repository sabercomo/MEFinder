@echo off
setlocal
cd /d "%~dp0"

for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
  taskkill /F /PID %%p >nul 2>nul
)

echo Starting ME Finder at http://127.0.0.1:8765/
echo Keep this window open while searching.
py -3 -m src.me_finder serve --host 127.0.0.1 --port 8765

pause
