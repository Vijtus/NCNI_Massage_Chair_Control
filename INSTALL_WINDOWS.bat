@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul && (
  py -3 install.py
  goto :end
)
where python >nul 2>nul && (
  python install.py
  goto :end
)

echo.
echo Python 3 was not found.
echo Install Python 3 from https://www.python.org/downloads/ and run again.
echo.
pause
exit /b 1

:end
echo.
echo Install finished. Press any key to close.
pause >nul
