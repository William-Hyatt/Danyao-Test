@echo off
setlocal

cd /d "%~dp0"
title Hyatt Comp Night Scanner

echo Hyatt Comp Night Scanner
echo Terminal version - no local website UI will be started.
echo.

where py >nul 2>nul
if not errorlevel 1 (
    py "%~dp0console_scan.py"
) else (
    python "%~dp0console_scan.py"
)

set EXIT_CODE=%ERRORLEVEL%
echo.
if not "%EXIT_CODE%"=="0" echo Scanner exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
