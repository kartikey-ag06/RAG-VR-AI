@echo off
rem =====================================================================================================
rem  SETUP_DEV.bat - one-time setup for developers on Windows. Safe to run again at any time.
rem    1. checks Python 3.11+ and Node.js 20.19+
rem    2. creates backend\.venv and installs the Python packages (+ test tools)
rem    3. installs the VR app's packages (npm ci) and builds it into glasses_app\dist
rem    4. creates .env from .env.example (your settings file - never committed)
rem    5. runs the quick tests
rem  Afterwards: START_VR_RAG.bat starts the server on http://localhost:8765
rem =====================================================================================================
setlocal
title VR_RAG developer setup
cd /d "%~dp0"

echo.
echo [1/5] Checking tools...
set "PY="
if exist "backend\.venv\Scripts\python.exe" set "PY=backend\.venv\Scripts\python.exe"
if not defined PY (where py >nul 2>nul && set "PY=py -3")
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>nul || goto oldpython
for /f "delims=" %%v in ('%PY% -c "import sys; print(sys.version.split()[0])"') do echo       Python %%v - OK

where node >nul 2>nul || goto nonode
node -e "const [a,b]=process.versions.node.split('.').map(Number); process.exit(a>20||(a===20&&b>=19)?0:1)" || goto oldnode
for /f "delims=" %%v in ('node --version') do echo       Node.js %%v - OK
where git >nul 2>nul && (for /f "delims=" %%v in ('git --version') do echo       %%v - OK)

echo.
echo [2/5] Python packages (first time: a few minutes)...
if not exist "backend\.venv\Scripts\python.exe" (
  %PY% -m venv backend\.venv || goto fail
)
backend\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet || goto fail
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt --quiet || goto fail
rem same marker START_VR_RAG.bat uses, so it will not reinstall
copy /y "backend\requirements.txt" "backend\.venv\installed.req" >nul
echo       OK

echo.
echo [3/5] VR app packages and build...
pushd glasses_app
call npm ci --no-audit --no-fund || (popd & goto fail)
call npm run build || (popd & goto fail)
popd
echo       OK - built into glasses_app\dist

echo.
echo [4/5] Settings file...
if exist ".env" (
  echo       .env already exists - kept as it is
) else (
  copy /y ".env.example" ".env" >nul
  echo       Created .env from .env.example
)
findstr /r /c:"^GROQ_API_KEY=..*" ".env" >nul 2>nul
if errorlevel 1 if not exist "grok_api_key.txt" (
  echo       ACTION NEEDED: open .env and put your own Groq key after GROQ_API_KEY=
  echo                      free key: https://console.groq.com/keys
)

echo.
echo [5/5] Quick tests...
backend\.venv\Scripts\python.exe -m pytest -q tests\test_limits.py -p no:cacheprovider || goto testfail

echo.
echo =====================================================================================
echo  Setup finished.
echo    Start the app:   START_VR_RAG.bat        then open http://localhost:8765
echo    Live-edit the VR app:  cd glasses_app  and  npm run dev
echo    All tests:       backend\.venv\Scripts\python.exe -m pytest -q tests
echo    How we work:     CONTRIBUTING.md
echo =====================================================================================
pause
exit /b 0

:nopython
echo  Python 3.11 or newer is not installed. Get it from https://www.python.org/downloads/
echo  (tick "Add python.exe to PATH" in the installer), then run this file again.
goto end
:oldpython
echo  Your Python is older than 3.11. Install 3.11+ from https://www.python.org/downloads/ and run again.
goto end
:nonode
echo  Node.js is not installed. Get the LTS version (20.19 or newer) from https://nodejs.org and run again.
goto end
:oldnode
echo  Your Node.js is older than 20.19. Install the LTS version from https://nodejs.org and run again.
goto end
:testfail
echo.
echo  Setup worked, but a quick test failed - see the output above, or ask in the team chat.
goto end
:fail
echo.
echo  Setup stopped with an error - see the message above. Run SETUP_DEV.bat again after fixing it.
:end
pause
exit /b 1
