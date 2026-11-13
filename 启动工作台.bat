@echo off
cd /d "%~dp0"
if exist "runtime\pythonw.exe" (
  start "" "runtime\pythonw.exe" -E -s -B -X utf8 "Workbench\scripts\portable_startup.py"
) else (
  python -B -X utf8 "Workbench\scripts\portable_startup.py"
)
