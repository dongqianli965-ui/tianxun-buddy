@echo off
set TXBUDDY_NO_PAUSE=1
call "%~dp0CHECK_V426_FRONTEND_MANUAL.cmd" --no-pause
exit /b %ERRORLEVEL%
