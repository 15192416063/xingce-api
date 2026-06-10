@echo off
title Xingce - Backend Server
cd /d "%~dp0"
echo ========================================================
echo   Xingce Question Bank - Start Backend (port 8000)
echo   First run downloads embedding model (~400MB), please wait.
echo   When you see "Application startup complete", it is READY.
echo   Then double-click  tunnel.bat  to expose it online.
echo ========================================================
echo.
uvicorn main:app --port 8000
pause
