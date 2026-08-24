@echo off
cd /d "%~dp0"
set MAVERICK_AGENT=claude
python bridge.py
pause
