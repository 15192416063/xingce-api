@echo off
title Xingce - Public Tunnel
cd /d "%~dp0"
echo ========================================================
echo   Xingce - Public Tunnel (cloudflared, free)
echo   Make sure the backend is running first (port 8000).
echo ========================================================
echo.
if not exist cloudflared.exe (
  echo Downloading cloudflared ... first time only, please wait.
  powershell -Command "try{[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile 'cloudflared.exe'}catch{exit 1}"
)
if not exist cloudflared.exe (
  echo.
  echo [Download failed] GitHub may be blocked in your network.
  echo Download manually from:
  echo   https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
  echo Rename it to  cloudflared.exe  and put it in this folder, then run again.
  echo China alternative: natapp.cn  (more stable, fixed URL^)
  pause
  exit /b
)
echo.
echo Building tunnel ... a line like
echo    https://xxxx.trycloudflare.com
echo will appear below. That is your PUBLIC URL - share it with testers.
echo Close this window to STOP the tunnel.
echo ========================================================
echo.
cloudflared.exe tunnel --url http://localhost:8000
pause
