@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>nul
    if errorlevel 1 (python -m venv .venv) else (py -3 -m venv .venv)
    if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -c "import requests, bs4, playwright, pypdf" >nul 2>nul
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto fail
".venv\Scripts\python.exe" serve.py --open
if errorlevel 1 goto fail
exit /b 0
:fail
pause
exit /b 1
