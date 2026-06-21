@echo off
title Xingce Backend
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto setup
goto run

:setup
echo ============================================================
echo   First run: creating .venv and installing dependencies.
echo   This may take a few minutes. Please wait...
echo ============================================================
where py >nul 2>nul
if %errorlevel%==0 (py -3 -m venv .venv) else (python -m venv .venv)
if not exist ".venv\Scripts\python.exe" goto noenv
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt

:run
echo ============================================================
echo   Xingce backend starting on port 8000 ...
echo   Wait for:  Application startup complete
echo   Then open in browser:  http://127.0.0.1:8000
echo   Close this window to STOP the server.
echo ============================================================
echo.
".venv\Scripts\python.exe" -m uvicorn main:app --port 8000
goto end

:noenv
echo [ERROR] Could not create .venv automatically.
echo Install Python 3.11+ from python.org (check "Add Python to PATH"),
echo then double-click this file again.

:end
pause
