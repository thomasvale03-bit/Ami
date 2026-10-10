@echo off
setlocal
cd /d "%~dp0"
title Playmaker Jarvis - Check Now
if not exist logs mkdir logs

where node >nul 2>nul
if errorlevel 1 (
  echo Node.js is not installed. Run Setup-Jarvis.bat first.
  goto :failed
)
if not exist node_modules\playwright-core (
  echo Jarvis is not installed yet. Run Setup-Jarvis.bat first.
  goto :failed
)

echo Jarvis is checking yesterday's TAO validations. This can take a few minutes...
echo.
node check.js %*
if errorlevel 1 goto :failed

echo.
echo ============================================================
echo  Jarvis check finished successfully. The dashboard is updated.
echo ============================================================
pause
exit /b 0

:failed
echo.
echo ============================================================
echo  Jarvis check did NOT finish successfully.
echo  Read the messages above. Details were saved to:
echo    %~dp0logs\jarvis-error.log
echo  Screenshots (if any) are in:
echo    %~dp0logs\screenshots
echo ============================================================
pause
exit /b 1
