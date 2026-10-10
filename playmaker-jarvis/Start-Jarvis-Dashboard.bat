@echo off
setlocal
cd /d "%~dp0"
title Playmaker Jarvis - Dashboard
if not exist logs mkdir logs
set "URL=http://127.0.0.1:8787"

where node >nul 2>nul
if errorlevel 1 (
  echo Node.js is not installed. Run Setup-Jarvis.bat first.
  pause
  exit /b 1
)

curl.exe -s -o nul -m 2 %URL%/api/health
if not errorlevel 1 goto :open

echo Starting the Jarvis dashboard server...
start "Playmaker Jarvis Server (keep this open)" /min cmd /k "node server.js || (echo. & echo Dashboard server stopped with an error. Details: logs\jarvis-error.log & pause)"

for /l %%i in (1,1,20) do (
  curl.exe -s -o nul -m 1 %URL%/api/health
  if not errorlevel 1 goto :open
  timeout /t 1 /nobreak >nul
)
echo.
echo The dashboard server did not start. Check the minimized "Playmaker Jarvis Server" window
echo and %~dp0logs\jarvis-error.log
pause
exit /b 1

:open
start "" msedge --new-window --app=%URL% --start-fullscreen
exit /b 0
