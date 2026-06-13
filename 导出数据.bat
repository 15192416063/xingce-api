@echo off
title Export public question-bank seed and upload to GitHub Release
cd /d "%~dp0xingce_api"
echo ========================================================
echo   Export public question-bank seed (data goes to Release,
echo   NOT into git. Code is pushed separately via 更新代码.bat)
echo ========================================================
echo.
python export_public.py
echo.
echo ========================================================
echo   Copy the "gh release create ..." line printed above and
echo   run it here to upload, OR drag the zip into:
echo   https://github.com/15192416063/xingce-api/releases  (Draft new release)
echo ========================================================
pause
