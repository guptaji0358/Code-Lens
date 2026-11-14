@echo off
rem Rebuild CodeLens binaries (only if sources changed) and launch the app.
rem   build_and_run.bat            incremental: skips the build when nothing changed
rem   build_and_run.bat --force    rebuild everything
rem   build_and_run.bat --no-installer / --no-launch   (flags pass through)
rem Same logic is exposed to the AI tooling as the LIAS automation "build-release".
setlocal
cd /d "%~dp0"
python scripts\build_release.py %*
if errorlevel 1 (
    echo.
    echo Build FAILED - see output above.
    pause
    exit /b 1
)
endlocal
