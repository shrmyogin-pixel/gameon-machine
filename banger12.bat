@echo off
rem Up to 12 dense Shorts across all watchlist niches (nothing is uploaded). e.g. banger12.bat --count 3
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
title gameon-clipper (banger 12)
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found. Create it first: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
echo ==== Generating up to 12 Shorts - this takes a long while ====
.venv\Scripts\python.exe -m clipper news --all-niches --count 12 --min-videos 12 --min-photos 22 --no-promo %*
set CODE=%ERRORLEVEL%
echo.
echo Finished (exit code %CODE%). Read the summary above.
pause
exit /b %CODE%
