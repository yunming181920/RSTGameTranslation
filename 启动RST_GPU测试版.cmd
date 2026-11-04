@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_rst_gpu.ps1"
if errorlevel 1 pause
