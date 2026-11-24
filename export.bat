@echo off
rem One-click export after you have reviewed the clips
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
title gameon-clipper (export)
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found. Create it first: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
echo ==== Exporting reviewed clips ====
.venv\Scripts\python.exe -m clipper export %*
set CODE=%ERRORLEVEL%
echo.
echo Finished (exit code %CODE%). Read the summary above.
pause
exit /b %CODE%
