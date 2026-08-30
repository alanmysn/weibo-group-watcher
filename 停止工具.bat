@echo off
setlocal
set "STOP_URL=http://127.0.0.1:8765/api/prepare-stop"

powershell -NoProfile -Command "try { $s = Invoke-RestMethod -Method Post -Uri '%STOP_URL%' -TimeoutSec 2; if (-not $s.ok -or -not $s.pid) { exit 1 }; Stop-Process -Id ([int]$s.pid) -Force; exit 0 } catch { exit 1 }" >nul 2>nul
if errorlevel 1 (
    echo The watcher is not running, or it could not be stopped.
    pause
    exit /b 1
)
exit /b 0
