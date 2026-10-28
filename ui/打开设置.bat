@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   [ERROR] Python not found in PATH.
  echo   Install Python, or add it to PATH, then run this again.
  echo.
  pause
  exit /b 1
)
python serve.py
if errorlevel 1 (
  echo.
  echo   Server stopped with an error. See the messages above.
  pause
)
