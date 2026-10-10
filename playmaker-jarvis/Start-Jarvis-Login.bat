@echo off
setlocal
cd /d "%~dp0"
title Playmaker Jarvis - TAO Sign-in
if not exist logs mkdir logs
where node >nul 2>nul
if errorlevel 1 (
  echo Node.js is not installed. Run Setup-Jarvis.bat first.
  pause
  exit /b 1
)
node login.js
if errorlevel 1 (
  echo.
  echo Sign-in helper stopped with an error. Details: %~dp0logs\jarvis-error.log
  pause
  exit /b 1
)
pause
