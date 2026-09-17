@echo off
REM ===========================================================================
REM  XiaoMa AI Studio - Windows one-click launcher
REM
REM  IMPORTANT: keep this file pure ASCII.
REM  cmd.exe parses batch files by byte offset. With `chcp 65001` plus multi-byte
REM  (i.e. Chinese) text in the file, those offsets desynchronise and cmd starts
REM  executing fragments of lines - real symptom: `'cho.' is not recognized`.
REM  The failure messages themselves blew up, so users saw nothing useful.
REM  All user-facing Chinese therefore lives in backend\tools\env_report.py,
REM  which prints through Python (correct on every console code page).
REM ===========================================================================
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"

set "ROOT=%~dp0"
set "VENV_PY=%ROOT%backend\venv\Scripts\python.exe"
set "CHECK=%ROOT%backend\tools\env_report.py"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

echo ================================================
echo   XiaoMa AI Studio - one-click start (Windows)
echo ================================================
echo.

REM ---------- pre-check: a python that actually works ----------
REM Deliberately NOT using `where python`: that only proves the file exists and
REM depends on PATH being sane. Running it is the real test, and it also catches
REM the Microsoft Store stub (which just opens the Store and returns nothing).
python -c "import sys" >nul 2>nul
if errorlevel 1 (
  python "%CHECK%" --hint no-python 2>nul
  if errorlevel 1 (
    echo [ERROR] Python 3.11+ not found, or the installed one cannot run.
    echo         Install from https://www.python.org/downloads/ and check
    echo         "Add python.exe to PATH", then reopen this window.
  )
  pause
  exit /b 1
)

REM ---------- step 0: environment preflight ----------
echo [0/4] checking environment ...
python "%CHECK%"
if errorlevel 1 (
  echo.
  python "%CHECK%" --hint env-check
  pause
  exit /b 1
)
echo.

REM ---------- step 1: virtual environment ----------
if not exist "%VENV_PY%" (
  echo [1/4] creating Python virtual environment ...
  python -m venv "%ROOT%backend\venv"
) else (
  echo [1/4] virtual environment already present, skipping.
)

REM Fallback: uv can build a virtual environment even when the interpreter's own
REM venv module is unavailable. Embedded / stripped Python builds (bundled with
REM many all-in-one toolchains) ship without venv, and that is the single most
REM common reason this step fails. --seed is required: a plain `uv venv` has no
REM pip, and step 2 below drives pip through the venv interpreter.
if not exist "%VENV_PY%" (
  echo       python -m venv did not work, trying uv ...
  call uv --version >nul 2>nul
  if not errorlevel 1 call uv venv --seed --python 3.11 "%ROOT%backend\venv"
)

REM Exit code 0 does not prove the venv is usable, so verify it.
if not exist "%VENV_PY%" (
  python "%CHECK%" --hint venv-failed
  pause
  exit /b 1
)

REM ---------- step 2: backend dependencies ----------
echo [2/4] installing backend dependencies ...
"%VENV_PY%" -m pip install --quiet --disable-pip-version-check --upgrade pip
if errorlevel 1 (
  echo.
  python "%CHECK%" --hint pip-upgrade
  pause
  exit /b 1
)
"%VENV_PY%" -m pip install --quiet --disable-pip-version-check -r "%ROOT%backend\requirements.txt"
if errorlevel 1 (
  echo.
  python "%CHECK%" --hint pip-install
  pause
  exit /b 1
)

REM ---------- step 3: frontend dependencies ----------
if not exist "%ROOT%frontend\node_modules" (
  echo [3/4] installing frontend dependencies ^(first run only^) ...
  call npm --version >nul 2>nul
  if errorlevel 1 (
    python "%CHECK%" --hint npm-missing
    pause
    exit /b 1
  )
  pushd "%ROOT%frontend"
  call npm install
  if errorlevel 1 (
    popd
    echo.
    python "%CHECK%" --hint npm-install
    pause
    exit /b 1
  )
  popd
) else (
  echo [3/4] frontend dependencies already present, skipping.
)

REM ---------- step 4: build the frontend ----------
echo [4/4] building the frontend ...
pushd "%ROOT%frontend"
call npm run build
if errorlevel 1 (
  popd
  echo.
  python "%CHECK%" --hint npm-build
  pause
  exit /b 1
)
popd

echo.
echo ================================================
echo   Open in your browser:  http://127.0.0.1:8787
echo   Press Ctrl+C to stop the service.
echo ================================================
echo.

set "FRONTEND_DIST=%ROOT%frontend\webroot"
"%VENV_PY%" -m uvicorn app.main:app --app-dir "%ROOT%backend" --host 127.0.0.1 --port 8787
if errorlevel 1 (
  echo.
  python "%CHECK%" --hint serve-failed
  pause
  exit /b 1
)

endlocal
