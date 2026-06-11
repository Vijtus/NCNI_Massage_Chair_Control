@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" app.py %*
) else (
  py -3 app.py %*
)

if errorlevel 1 (
  echo.
  echo The panel stopped with an error. Check verification_report.txt or run install.py.
  pause
)
