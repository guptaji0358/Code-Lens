@echo off
rem Git automation launcher: add paths -> commit -> push -> verify log.
rem Usage: double-click, or pass args through to tools\git_auto.py
rem   git_auto.bat --paths scripts\a.py --message "Fix X" --log short
setlocal
where python >nul 2>nul || (echo [FAILED] python not found on PATH & exit /b 1)
python "%~dp0tools\git_auto.py" %*
set RC=%ERRORLEVEL%
if "%~1"=="" pause
exit /b %RC%
