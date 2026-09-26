@echo off
title Discord Export Viewer
setlocal
if "%DM_PORT%"=="" set DM_PORT=5055
cd /d "%~dp0"

rem already running? just open the browser
powershell -NoProfile -Command "try{Invoke-RestMethod -Uri \"http://127.0.0.1:%DM_PORT%/api/stats\" -TimeoutSec 2|Out-Null; exit 0}catch{exit 1}"
if %errorlevel%==0 (
  start "" "http://127.0.0.1:%DM_PORT%"
  exit /b
)

start "" "http://127.0.0.1:%DM_PORT%"
python server.py
