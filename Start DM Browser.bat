@echo off
title DM Browser
setlocal
if "%DM_PORT%"=="" set DM_PORT=5055
cd /d "%~dp0"

rem already running? just open the browser
powershell -NoProfile -Command "try{Invoke-RestMethod -Uri \"http://127.0.0.1:%DM_PORT%/api/stats\" -TimeoutSec 2|Out-Null; exit 0}catch{exit 1}"
if %errorlevel%==0 (
  start "" "http://127.0.0.1:%DM_PORT%"
  exit /b
)

rem first run? build the index
if not exist dm.db (
  echo First run — building the search index from your data package...
  python build_dm_db.py
  if not exist dm.db (
    echo.
    echo Could not find your Discord data package.
    echo Create config.json next to this file:  {"package": "C:\\path\\to\\package"}
    echo ^(point it at the folder extracted from request_data.zip — the one with Messages/ inside^)
    pause
    exit /b 1
  )
  echo.
  echo Want images and files cached locally too? Run:  python fetch_attachments.py
  echo ^(Optional, may take a while. The browser works without it.^)
  echo.
)

start "" "http://127.0.0.1:%DM_PORT%"
python server.py
