@echo off
cd /d "%~dp0"
set MAVERICK_AGENT=mock
python bridge.py
pause
