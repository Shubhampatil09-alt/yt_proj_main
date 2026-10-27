@echo off
setlocal EnableExtensions
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0save_youtube_key.ps1"
if errorlevel 1 (
  echo ERROR: The YouTube API key was not saved.
  exit /b 1
)
exit /b 0
