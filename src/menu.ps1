[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.WindowTitle = "全電腦檔案智慧管理控制中心 - Omni-Organizer v3"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $projectRoot ".venv\Scripts\python.exe"
$venvPyw = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
$pyExe = if (Test-Path $venvPy) { $venvPy } else { "python" }
$pywExe = if (Test-Path $venvPyw) { $venvPyw } else { "pythonw.exe" }
$configFile = Join-Path $projectRoot "config\config.yaml"
$logFile = Join-Path $projectRoot "logs\organize.log"
$manageScript = Join-Path $PSScriptRoot "manage_service.py"
$auditScript = Join-Path $projectRoot "scripts\deep_audit.py"
$archMap = Join-Path $projectRoot "docs\pc_architecture_map.md"

function Show-Header {
    Clear-Host
    Write-Host "================================================================" -ForegroundColor Cyan
    Write-Host "          [全電腦檔案智慧管理控制中心] (Omni-Organizer v3)" -ForegroundColor Yellow
    Write-Host "================================================================" -ForegroundColor Cyan

    $statusOutput = & $pyExe $manageScript status 2>$null
    $isSrv = ($statusOutput | Select-String "SERVICE_RUNNING=YES")
    $pidsLine = ($statusOutput | Select-String "SERVICE_PIDS=(.*)").Matches.Groups[1].Value
    $isAuto = ($statusOutput | Select-String "AUTOSTART=YES")

    if ($isSrv) {
        Write-Host "   [背景即時整理服務] : " -NoNewline
        Write-Host "[ON] 正在背景運行中 (PID: $pidsLine)" -ForegroundColor Green
    } else {
        Write-Host "   [背景即時整理服務] : " -NoNewline
        Write-Host "[OFF] 目前未在運行 (按 6 可啟動)" -ForegroundColor DarkGray
    }

    if ($isAuto) {
        Write-Host "   [Windows 開機自啟] : " -NoNewline
        Write-Host "[ON] 已啟用 (開機自動在背景啟動監控服務)" -ForegroundColor Green
    } else {
        Write-Host "   [Windows 開機自啟] : " -NoNewline
        Write-Host "[OFF] 未啟用 (按 8 可啟用)" -ForegroundColor DarkGray
    }

    Write-Host "`n   [目前管理路徑清單] :" -ForegroundColor Cyan
    $pathsOutput = & $pyExe $manageScript list_paths 2>$null
    $rawPaths = ($pathsOutput | Select-String "CURRENT_PATHS=(.*)").Matches.Groups[1].Value
    if ($rawPaths) {
        $pathList = $rawPaths -split ";"
        for ($i = 0; $i -lt $pathList.Count; $i++) {
            Write-Host "      $($i + 1). $($pathList[$i])" -ForegroundColor Gray
        }
    } else {
        Write-Host "      (無)" -ForegroundColor DarkGray
    }

    Write-Host "================================================================" -ForegroundColor Cyan
    Write-Host "   [1] 立即執行完整整理 (realtime + maintenance)" -ForegroundColor White
    Write-Host "   [2] 模擬預覽整理效果 (Dry-run Simulation，不實際改動)" -ForegroundColor White
    Write-Host "   [3] 僅執行即時分流 (--tags realtime) 或清道夫維護 (--tags maintenance)" -ForegroundColor White
    Write-Host "   [4] 新增要監控的資料夾路徑" -ForegroundColor White
    Write-Host "   [5] 移除受監控的資料夾路徑" -ForegroundColor White
    Write-Host "   [6] 啟動 / 重啟背景自動監控" -ForegroundColor White
    Write-Host "   [7] 停止背景自動監控" -ForegroundColor White
    Write-Host "   [8] 切換開機自動啟動 (啟用 / 停用)" -ForegroundColor White
    Write-Host "   [9] 執行全機唯讀深度健檢 (scripts\deep_audit.py)" -ForegroundColor White
    Write-Host "   [P] 開啟桌面審核確認視窗 (src\desktop_prompt.py)" -ForegroundColor White
    Write-Host "   [C] 編輯全域分類規則 (config\config.yaml)" -ForegroundColor Gray
    Write-Host "   [L] 查看執行日誌 (logs\organize.log)" -ForegroundColor Gray
    Write-Host "   [M] 查看全機目錄地圖 (docs\pc_architecture_map.md)" -ForegroundColor Gray
    Write-Host "   [0] 離開" -ForegroundColor DarkGray
    Write-Host "================================================================" -ForegroundColor Cyan
}

while ($true) {
    Show-Header
    $choice = Read-Host "請輸入選項代碼"
    if ([string]::IsNullOrWhiteSpace($choice)) { exit }

    switch ($choice.Trim().ToUpper()) {
        "1" {
            Clear-Host
            Write-Host "正在掃描並執行完整整理 (config\config.yaml)..." -ForegroundColor Yellow
            & $pyExe -m organize run "$configFile"
            Write-Host "`n整理完成！" -ForegroundColor Green
            pause
        }
        "2" {
            Clear-Host
            Write-Host "正在模擬預覽整理結果 (Dry-run，不改動任何檔案)..." -ForegroundColor Yellow
            & $pyExe -m organize sim "$configFile"
            pause
        }
        "3" {
            Clear-Host
            Write-Host "請選擇執行標籤：" -ForegroundColor Yellow
            Write-Host "  [R] 僅執行即時分流 (--tags realtime)"
            Write-Host "  [M] 僅執行每日清道夫維護 (--tags maintenance)"
            $tagChoice = (Read-Host "選擇 [R/M]").Trim().ToUpper()
            if ($tagChoice -eq "R") {
                & $pyExe -m organize run --tags realtime "$configFile"
            } elseif ($tagChoice -eq "M") {
                & $pyExe -m organize run --tags maintenance "$configFile"
            } else {
                Write-Host "已取消。" -ForegroundColor DarkGray
            }
            pause
        }
        "4" {
            Clear-Host
            $newPath = Read-Host "請輸入完整資料夾路徑 (例如 D:\工作資料)"
            if (-not [string]::IsNullOrWhiteSpace($newPath)) {
                & $pyExe $manageScript add_path $newPath.Trim('"')
            }
            pause
        }
        "5" {
            Clear-Host
            $remPath = Read-Host "請輸入要移除的完整路徑"
            if (-not [string]::IsNullOrWhiteSpace($remPath)) {
                & $pyExe $manageScript remove_path $remPath.Trim('"')
            }
            pause
        }
        "6" {
            Clear-Host
            & $pyExe $manageScript restart
            pause
        }
        "7" {
            Clear-Host
            & $pyExe $manageScript stop
            pause
        }
        "8" {
            Clear-Host
            $statusOutput = & $pyExe $manageScript status 2>$null
            if ($statusOutput | Select-String "AUTOSTART=YES") {
                & $pyExe $manageScript disable_auto
            } else {
                & $pyExe $manageScript enable_auto
            }
            pause
        }
        "9" {
            Clear-Host
            Write-Host "正在執行全機唯讀深度健檢..." -ForegroundColor Yellow
            & $pyExe $auditScript
            pause
        }
        "P" {
            Start-Process $pywExe -ArgumentList "`"$PSScriptRoot\desktop_prompt.py`"" -WorkingDirectory $projectRoot
        }
        "C" {
            Start-Process notepad.exe $configFile
        }
        "L" {
            if (-not (Test-Path $logFile)) {
                New-Item -Path $logFile -ItemType File -Force | Out-Null
            }
            Start-Process notepad.exe $logFile
        }
        "M" {
            if (Test-Path $archMap) {
                Start-Process notepad.exe $archMap
            } else {
                Write-Host "找不到地圖文件：$archMap" -ForegroundColor Red
                pause
            }
        }
        "0" {
            exit
        }
    }
}
