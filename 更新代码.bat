@echo off
title Commit and Push to GitHub
cd /d "%~dp0"
echo ========================================================
echo   Commit and push your code to GitHub
echo ========================================================
echo.
git add -A
echo.
set /p MSG=Describe what you changed (then press Enter):
if "%MSG%"=="" set MSG=update
git commit -m "%MSG%"
echo.
echo Pushing to GitHub ...
git push
echo.
echo ========================================================
echo   Done. Check: https://github.com/15192416063/xingce-api
echo ========================================================
pause
