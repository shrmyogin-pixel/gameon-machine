@echo off
rem Gaming tech Shorts (nothing is uploaded). Extra options pass through, e.g. tech.bat --count 2
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
title gameon-clipper (gaming tech only)
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found. Create it first: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
echo ==== Generating gaming tech Shorts - this can take a while ====
.venv\Scripts\python.exe -m clipper news --topic tech %*
set CODE=%ERRORLEVEL%
echo.
echo Finished (exit code %CODE%). Read the summary above.
pause
exit /b %CODE%
