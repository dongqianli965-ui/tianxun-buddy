@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "ROOT=%~dp0"
set "PYTHON="
set "PYTHON_ARGS="
if defined TXBUDDY_MANUAL_PYTHON if exist "%TXBUDDY_MANUAL_PYTHON%" set "PYTHON=%TXBUDDY_MANUAL_PYTHON%"
if not defined PYTHON if exist "%ROOT%..\_CODEX_V424\venv\Scripts\python.exe" set "PYTHON=%ROOT%..\_CODEX_V424\venv\Scripts\python.exe"
if not defined PYTHON if exist "%ROOT%.venv\Scripts\python.exe" set "PYTHON=%ROOT%.venv\Scripts\python.exe"
if not defined PYTHON for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYTHON set "PYTHON=%%P"
if not defined PYTHON for /f "delims=" %%P in ('where py 2^>nul') do if not defined PYTHON (
  set "PYTHON=%%P"
  set "PYTHON_ARGS=-3"
)
if not defined PYTHON (
  echo BLOCKED: Python not found. Set TXBUDDY_MANUAL_PYTHON to a Python executable.
  set "RC=2"
  goto done
)
echo [V4.2.6 TEST_ONLY] stop manual acceptance environment
pushd "%ROOT%"
if errorlevel 1 (
  set "RC=2"
  goto done
)
"%PYTHON%" %PYTHON_ARGS% "scripts\v426_manual_runtime.py" stop
set "RC=%ERRORLEVEL%"
popd
:done
if not "%TXBUDDY_NO_PAUSE%"=="1" if /I not "%~1"=="--no-pause" pause
endlocal & exit /b %RC%
