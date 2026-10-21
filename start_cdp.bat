@echo off
echo === Football Hedge P0 - Auto Start ===
echo.
echo [1/2] Starting Chrome CDP on :9222...
start "" "C:/Program Files/Google/Chrome/Application/chrome.exe" --remote-debugging-port=9222 https://m829.crw066.com
echo Chrome launched if Chrome is installed.
echo.
echo [2/2] Starting backend on :8030...
cd /d "C:/1/football_hedge_p0/football_hedge_p0"
start "" ".venv/Scripts/python.exe" -m uvicorn backend.main:app --host 127.0.0.1 --port 8030
echo Backend starting...
timeout /t 3 >nul
start http://127.0.0.1:8030
echo.
echo === Ready ===
pause