@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

set "PY_EXE=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY_EXE=%~dp0.venv\Scripts\python.exe"

echo ================================================================
echo   Omni-Organizer v3 運作狀態檢查
echo ================================================================
"%PY_EXE%" "%~dp0src\manage_service.py" status
echo.
echo --- 規則語法驗證 (organize check) ---
"%PY_EXE%" -m organize check "%~dp0config\config.yaml"
echo.
echo --- 模擬預覽 (organize sim) ---
"%PY_EXE%" -m organize sim "%~dp0config\config.yaml"
echo.
echo --- 最近 15 筆執行日誌 ---
powershell -NoProfile -Command "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; if (Test-Path '%~dp0logs\organize.log') { Get-Content '%~dp0logs\organize.log' -Tail 15 -Encoding UTF8 } else { '(尚無日誌)' }"
echo ================================================================
pause
