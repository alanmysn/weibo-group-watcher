@echo off
setlocal
set "PANEL_URL=http://127.0.0.1:8765"

rem If the panel is already healthy, only open it.
call :panel_ready
if not errorlevel 1 goto open_panel

rem Start without a persistent console window, then wait for Flask to be ready.
start "weibo-group-watcher" /D "%~dp0" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0main.py" run
for /L %%i in (1,1,20) do (
    timeout /t 1 /nobreak >nul
    call :panel_ready
    if not errorlevel 1 goto open_panel
)

echo The panel did not start. Check logs\watcher.log for details.
pause
exit /b 1

:open_panel
start "" "%PANEL_URL%"
exit /b 0

:panel_ready
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri '%PANEL_URL%/api/state' -TimeoutSec 1; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; exit 1" >nul 2>nul
exit /b %errorlevel%
