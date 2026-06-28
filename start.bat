@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

title TikTok Live Monitor

echo.
echo   TikTok Live Monitor
echo   ---------------------------------------
echo.

:: ── Argument handling ──────────────────────────────────────────────────────
:: Usage:
::   start.bat                        -> read users.txt (may be empty)
::   start.bat username               -> monitor one user this session only
::   start.bat user1 user2 user3 ...  -> monitor list this session only
::
:: Args are session-only and do NOT modify users.txt.

set ARG_USERS=
if not "%~1"=="" (
    echo   Session users from arguments:
    :arg_loop
    if "%~1"=="" goto arg_done
    echo     + %~1
    set ARG_USERS=!ARG_USERS! %~1
    shift
    goto arg_loop
    :arg_done
    echo.
)

:: ── Dependency checks ──────────────────────────────────────────────────────
echo   Checking dependencies...
echo.

set PREFLIGHT_OK=1

:: Python
python --version >nul 2>&1
if errorlevel 1 (
    echo   [X]  Python not found
    echo         ^> Install from https://python.org and add to PATH
    set PREFLIGHT_OK=0
) else (
    for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo   [OK] %%v
)

:: Flask
python -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo   [--] flask not installed - installing...
    python -m pip install --quiet flask
    python -c "import flask" >nul 2>&1
    if errorlevel 1 (
        echo   [X]  flask install failed - run: pip install flask
        set PREFLIGHT_OK=0
    ) else (
        echo   [OK] flask installed
    )
) else (
    echo   [OK] flask
)

:: streamlink
python -c "import streamlink" >nul 2>&1
if errorlevel 1 (
    echo   [--] streamlink not installed - installing...
    python -m pip install --quiet streamlink
    python -c "import streamlink" >nul 2>&1
    if errorlevel 1 (
        echo   [X]  streamlink install failed - run: pip install streamlink
        set PREFLIGHT_OK=0
    ) else (
        echo   [OK] streamlink installed
    )
) else (
    echo   [OK] streamlink
)

echo.
if "%PREFLIGHT_OK%"=="0" (
    echo   Fix the issues above and re-run.
    echo.
    pause
    exit /b 1
)

:: ── Auto-create users.txt if missing ───────────────────────────────────────
if not exist "users.txt" (
    echo # One TikTok username per line. Lines starting with # are ignored.> users.txt
    echo   [OK] Created empty users.txt
)

:: ── Auto-create save_location.txt if missing ───────────────────────────────
if not exist "save_location.txt" (
    echo %~dp0recordings> save_location.txt
    mkdir "%~dp0recordings" >nul 2>&1
    echo   [OK] Created save_location.txt -^> %~dp0recordings
) else (
    set /p SAVE_DIR=<save_location.txt
    echo   [OK] Save location: !SAVE_DIR!
)

echo.

:: ── Resolve user list ──────────────────────────────────────────────────────
set USERS=

if not "!ARG_USERS!"=="" (
    set USERS=!ARG_USERS!
) else (
    for /f "usebackq tokens=1 eol=#" %%u in ("users.txt") do (
        if not "%%u"=="" set USERS=!USERS! %%u
    )
    if "!USERS!"=="" (
        echo   users.txt is empty - starting with no users.
        echo   Add users via the web UI after it opens.
        echo.
    ) else (
        echo   Users from users.txt:!USERS!
        echo.
    )
)

:: ── Runtime dir ────────────────────────────────────────────────────────────
set RUNTIME_DIR=%TEMP%\tktm_%RANDOM%
mkdir "%RUNTIME_DIR%"
mkdir "%RUNTIME_DIR%\status"
mkdir "%RUNTIME_DIR%\pids"
mkdir "%RUNTIME_DIR%\logs"
mkdir "%RUNTIME_DIR%\paused"

set START_PORT=29044
if not defined SLEEP_INTERVAL set SLEEP_INTERVAL=60

echo   Starting...
echo.

python ui.py "%RUNTIME_DIR%" %START_PORT%!USERS!

echo.
echo   Monitor stopped.
pause
