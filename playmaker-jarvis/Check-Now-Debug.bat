@echo off
setlocal
cd /d "%~dp0"
title Playmaker Jarvis - Debug Check (visible)
echo Debug mode: an Edge window opens so you can watch every step.
echo Do not click inside that window while Jarvis is working.
echo.
call "%~dp0Check-Now.bat" --debug
