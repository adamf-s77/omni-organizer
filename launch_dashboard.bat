@echo off
chcp 65001 >nul
set "PYW_EXE=pythonw.exe"
if exist "%~dp0.venv\Scripts\pythonw.exe" set "PYW_EXE=%~dp0.venv\Scripts\pythonw.exe"
start "" "%PYW_EXE%" "%~dp0src\control_dashboard.py"
