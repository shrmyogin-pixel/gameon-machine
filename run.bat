@echo off
rem ============================================================================
rem  gameon-clipper unattended runner (leave this window open overnight)
rem
rem  - activates .venv
rem  - python -m clipper run   = watch + spike detection + clip workers +
rem                              headless Claude editing + hardware rendering
rem  - no automatic news: run news.bat by hand each night, then export.bat
rem  - keep-awake: the Python process asks Windows not to sleep while it runs
rem    (the screen may still turn off; closing a laptop lid or choosing Sleep
rem    yourself still sleeps the PC)
rem  - if the run exits with an error it is restarted after 15 seconds
rem  - Ctrl+C stops cleanly (finishes clips in progress). If Windows asks
rem    "Terminate batch job (Y/N)?" answer N, or Y: both are safe.
rem
rem  Extra arguments are passed on:  run.bat --only lacy
rem  Nothing is ever uploaded (autopost = false).
rem ============================================================================
setlocal
cd /d "%~dp0"
title gameon-clipper (run)

if not exist ".venv\Scripts\activate.bat" (
    echo ERROR: .venv not found. Create it first:  python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)
call ".venv\Scripts\activate.bat"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1

set RESTARTS=0
:loop
python -m clipper run %*
set CODE=%ERRORLEVEL%

if "%CODE%"=="0" goto clean
if "%CODE%"=="2" (
    echo.
    echo Exit code 2: bad arguments or config.toml could not be read. Fix that, then run run.bat again.
    exit /b 2
)
set /a RESTARTS+=1
echo.
echo [%date% %time%] clipper run exited with code %CODE%; restart #%RESTARTS% in 15 seconds ^(Ctrl+C to give up^)...
ping -n 16 127.0.0.1 >nul
goto loop

:clean
echo.
echo clipper stopped cleanly.
exit /b 0
