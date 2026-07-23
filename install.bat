@echo off
REM MeshPilot Windows Installer - launches the GUI installer (install-gui.ps1).
REM -sta is required for the folder-browse dialog to work correctly.
powershell -sta -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-gui.ps1"
if errorlevel 1 (
    echo.
    echo Installer exited with an error. See above for details.
    pause
)
