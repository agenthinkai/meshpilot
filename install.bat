@echo off
REM MeshPilot Windows Installer launcher — runs install.ps1 in the same folder.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause
