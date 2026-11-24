@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

set "PY_EXE=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY_EXE=%~dp0.venv\Scripts\python.exe"

echo 正在執行全機唯讀深度健檢 (deep_audit.py)...
"%PY_EXE%" "%~dp0scripts\deep_audit.py"
echo.
echo 健檢完成！報告已儲存至 reports\ 目錄。
pause
