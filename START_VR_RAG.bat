@echo off
title VR_RAG server
cd /d "%~dp0"
rem ---- 1) Python backend (uploads, embeddings, hybrid search). Sets up backend\.venv once,
rem         and again only when backend\requirements.txt changes. ----
if exist "backend\.venv\installed.req" (
  fc /b "backend\requirements.txt" "backend\.venv\installed.req" >nul 2>nul && goto run
)
set "PY="
if exist "backend\.venv\Scripts\python.exe" set "PY=backend\.venv\Scripts\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY (where py >nul 2>nul && set "PY=py")
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY goto ps
if not exist "backend\.venv\Scripts\python.exe" (
  echo First run: creating the Python environment...
  "%PY%" -m venv backend\.venv || goto ps
)
echo Installing / updating Python packages (one time, a few minutes)...
backend\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt || goto ps
copy /y "backend\requirements.txt" "backend\.venv\installed.req" >nul
:run
backend\.venv\Scripts\python.exe backend\app.py %*
if errorlevel 1 (echo. & echo Python backend stopped with an error - see above. & pause)
goto :eof
rem ---- 2) Fallback: PowerShell server (no uploads, keyword search only, no install needed) ----
:ps
echo Python not available - starting the PowerShell server instead (no uploads, keyword search only).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0server.ps1"
