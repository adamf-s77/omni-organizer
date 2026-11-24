# Omni-Organizer v3 - one-click user-layer autostart installer (no admin required)
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPy = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PyExe = if (Test-Path $VenvPy) { $VenvPy } else { "python" }
$Manage = Join-Path $ProjectRoot "src\manage_service.py"

Write-Host "=== 1. Register AtLogOn user task (OmniOrganizer_Watcher) ===" -ForegroundColor Cyan
& $PyExe $Manage enable_auto
Write-Host "=== 2. Restart watcher via Task Scheduler ===" -ForegroundColor Cyan
& $PyExe $Manage restart
Start-Sleep -Seconds 3
Write-Host "=== 3. Status ===" -ForegroundColor Cyan
& $PyExe $Manage status
