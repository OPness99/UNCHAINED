@echo off
title UNCHAINED - Starting...
cd /d "%~dp0"
echo Starting UNCHAINED...

REM Start Ollama server if installed but not running
where ollama >nul 2>&1
if %errorlevel%==0 (
    powershell -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2 -ErrorAction Stop; } catch { Start-Process ollama -ArgumentList 'serve' -WindowStyle Hidden }" >nul 2>&1
)

start "" "C:\Users\spart\AppData\Local\Programs\Python\Python313\python.exe" unchain.py
echo UNCHAINED launched.
timeout /t 2 /nobreak >nul
