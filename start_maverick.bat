@echo off
cd /d "%~dp0"
set MAVERICK_AGENT=codex
python bridge.py
pause
