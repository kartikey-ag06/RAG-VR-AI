@echo off
title VR_RAG server
cd /d "%~dp0"
rem ---- 1) Python backend (embeddings + hybrid search). First run creates backend\.venv once. ----
if exist "backend\.venv\installed.ok" goto run
set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY (where py >nul 2>nul && set "PY=py")
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY goto ps
echo First run: setting up the Python backend (one time, 2-5 minutes, ~250 MB)...
"%PY%" -m venv backend\.venv || goto ps
backend\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt || goto ps
echo ok> backend\.venv\installed.ok
:run
backend\.venv\Scripts\python.exe backend\app.py %*
if errorlevel 1 (echo. & echo Python backend stopped with an error - see above. & pause)
goto :eof
rem ---- 2) Fallback: PowerShell server (keyword search only, no install needed) ----
:ps
echo Python not available - starting the PowerShell server instead (keyword search only).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0server.ps1"
