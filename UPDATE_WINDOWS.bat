@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" update.py %*
) else (
  py -3 update.py %*
)

echo.
pause
