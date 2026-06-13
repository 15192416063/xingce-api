@echo off
chcp 65001 >nul
cd /d "%~dp0xingce_api"
python export_public.py
echo.
echo ====================================================
echo Done. Copy the "gh release create ..." line above to
echo upload, OR drag the zip into GitHub Releases (web).
echo ====================================================
pause
