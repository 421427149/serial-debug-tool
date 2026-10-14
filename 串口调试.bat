@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Keep this launcher ASCII-only so CMD can read it reliably before switching
rem to UTF-8. serial_debug.py itself is UTF-8 and may print Chinese text.
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

title Serial Debug Tool
cd /d "%~dp0"

rem Prefer python on PATH; fall back to the Windows Python launcher.
set "PYTHON_CMD="
python --version >nul 2>nul && set "PYTHON_CMD=python"
if not defined PYTHON_CMD (
    py -3 --version >nul 2>nul && set "PYTHON_CMD=py -3"
)
if not defined PYTHON_CMD (
    echo [ERROR] Python 3 was not found. Install it and enable "Add Python to PATH".
    pause
    exit /b 1
)

rem Install pyserial on first use. Do not continue if installation fails.
%PYTHON_CMD% -c "import serial" >nul 2>nul
if errorlevel 1 (
    echo pyserial is missing; installing it now...
    %PYTHON_CMD% -m pip install pyserial
    if errorlevel 1 (
        echo [ERROR] pyserial installation failed.
        pause
        exit /b 1
    )
)

rem Pass all caller arguments through unchanged, e.g. -p COM19 -b 1500000.
%PYTHON_CMD% "%~dp0serial_debug.py" %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" echo [ERROR] Serial tool exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
