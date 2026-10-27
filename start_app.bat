@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Local Bot Manager

echo [Local Bot Manager] Checking port 8000...
powershell -NoProfile -Command "$client = New-Object Net.Sockets.TcpClient; try { $client.Connect('127.0.0.1', 8000); exit 10 } catch { exit 0 } finally { $client.Dispose() }"
if errorlevel 10 (
  echo ERROR: 127.0.0.1:8000 is already in use. The application may already be running.
  echo Open http://127.0.0.1:8000/health or stop the existing process first.
  exit /b 10
)

set "PYTHON_CMD="
if exist ".venv\Scripts\python.exe" set "PYTHON_CMD=.venv\Scripts\python.exe"
if not defined PYTHON_CMD (
  where py >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=py -3"
)
if not defined PYTHON_CMD (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
  echo ERROR: Python was not found. Install Python 3.11 or newer, then create .venv.
  exit /b 2
)

echo [Local Bot Manager] Checking Python version...
%PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo ERROR: Python 3.11 or newer is required.
  exit /b 3
)

echo [Local Bot Manager] Checking dependencies...
%PYTHON_CMD% -c "import fastapi, httpx, jinja2, openpyxl, psutil, pydantic, selenium, uvicorn"
if errorlevel 1 (
  echo ERROR: One or more required packages are missing.
  echo Run: %PYTHON_CMD% -m pip install -r requirements.txt
  exit /b 4
)

%PYTHON_CMD% -c "from app.config import YOUTUBE_API_KEY; print('[Local Bot Manager] YouTube API: ' + ('configured' if YOUTUBE_API_KEY else 'not configured; run save_youtube_key.bat'))"
if /I "%~1"=="--check" (
  echo [Local Bot Manager] Startup checks passed. No server was started.
  exit /b 0
)

echo [Local Bot Manager] Starting http://127.0.0.1:8000
echo Press Ctrl+C in this window to stop cleanly and close managed browsers.
%PYTHON_CMD% main.py
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" echo ERROR: The application exited with code %APP_EXIT%.
exit /b %APP_EXIT%
