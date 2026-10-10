@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
start "" explorer "%~dp0logs"
