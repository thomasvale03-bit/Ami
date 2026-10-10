@echo off
rem Started by Windows Task Scheduler every day. Output goes to logs\scheduled-runs.log.
cd /d "%~dp0"
if not exist logs mkdir logs
node check.js --scheduled >> logs\scheduled-runs.log 2>&1
exit /b %errorlevel%
