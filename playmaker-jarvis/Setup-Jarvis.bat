@echo off
cd /d "%~dp0"
title Playmaker Jarvis - Setup
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Setup-Jarvis.ps1"
if errorlevel 1 (
  echo.
  echo Setup did not finish. Details: %~dp0logs\jarvis-error.log
  pause
)
